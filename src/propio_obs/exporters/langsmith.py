"""LangSmith integration — now thin over OpenTelemetry.

Before OTel migration this module owned the LangSmith RunTree lifecycle
(start/post/end/patch + binary attachment upload). After migration the
heavy lifting is gone: spans go through the shared OTel tracer in
`propio_obs.otel_init`, the OTLP exporter ships to a Collector, and the
Collector forwards to LangSmith's OTLP intake at
`https://api.smith.langchain.com/otel`.

What's left in this module:

- `configure(enabled, project, ...)` — sets `LANGSMITH_PROJECT` env so the
  Collector can stamp the `Langsmith-Project` header on every export.
- `decorate_request_span(span, ...)` / `decorate_child_span(span, ...)` —
  add LangSmith-recognised attributes (`langsmith.span.kind`,
  `langsmith.metadata.session_id`, etc.) to OTel spans the api.py verb
  layer creates.
- `wrap_openai(client)` — back-compat alias; redirects to
  `otel_init.instrument_openai_client` so OpenAI calls auto-emit OTel
  spans (replaces `langsmith.wrappers.wrap_openai`).
- Back-compat decorators (`traceable*`, `record_stt`, `record_tts`,
  `turn_trace`) — preserved as OTel-backed shims so legacy callers
  don't break, but new code should use the verb API.

What's gone:
- Audio attachments. OTel has no binary attachment primitive; LangSmith
  UI will not play voice audio until v0.2 S3 path lands. Passing
  `audio_wav=` triggers a one-time warning, then is ignored.
"""
from __future__ import annotations

import contextlib
import functools
import json
import logging
import os
from typing import Any, Callable, Optional


# Max chars for serialized input/output JSON stamped on a span. Bigger than
# the per-key [:2000] cap because LangSmith's Inputs/Outputs panels render
# the whole JSON tree; voice agents stay well under this, chat/tool agents
# with long message arrays may approach it. Truncation is best-effort —
# LangSmith renders truncated JSON as plain text without erroring.
_INOUT_MAX_CHARS = 16000

from .. import otel_init

logger = logging.getLogger(__name__)


# ─── Module state ────────────────────────────────────────────────
ENABLED: bool = False
_PROJECT: Optional[str] = None
# env is stamped onto every LangSmith-decorated span as
# `langsmith.metadata.env` so the UI's metadata filter works alongside the
# per-env project (double-key filtering — see config._resolve_langsmith_project).
_ENV: Optional[str] = None
_audio_warning_fired: bool = False


def configure(
    enabled: bool,
    project: Optional[str] = None,
    env: Optional[str] = None,
    api_key: str = "",
    endpoint: str = "",
) -> None:
    """Activate LangSmith export. Most parameters are now informational —
    the actual ship path lives in the OTel Collector. We still propagate
    `project` via env var so the Collector's `otlphttp/langsmith` exporter
    can stamp the Langsmith-Project header per request.
    """
    global ENABLED, _PROJECT, _ENV
    if not enabled:
        ENABLED = False
        return
    if project:
        os.environ.setdefault("LANGSMITH_PROJECT", project)
        _PROJECT = project
    _ENV = env
    ENABLED = True
    logger.info(
        f"[obs/langsmith] enabled (project={project or '<from env>'}, env={env or '<unset>'})"
    )


# ─── Verb-layer attribute decorators ─────────────────────────────
# Called from api.py — these mutate OTel spans the verb layer creates,
# adding LangSmith-recognised attribute conventions.

def _stamp_inputs(span: Any, inputs: dict) -> None:
    """Stamp inputs in BOTH conventions:

    - `input.value` (JSON string) — what LangSmith's OTel intake actually reads
      to populate the Inputs panel. OpenInference convention. SDK is
      schema-agnostic: whatever dict the agent passes gets serialized as-is.
    - `langsmith.inputs.{k}` (per-key string) — kept for Datadog tag-filter
      use and direct OTel attribute query. Cheap to write, doesn't conflict.
    """
    try:
        span.set_attribute(
            "input.value", json.dumps(inputs, default=str)[:_INOUT_MAX_CHARS]
        )
    except Exception as e:  # pragma: no cover
        logger.debug(f"[obs/langsmith] input.value serialize failed: {e}")
    for k, v in inputs.items():
        if v is not None:
            span.set_attribute(f"langsmith.inputs.{k}", str(v)[:2000])


def decorate_request_span(
    span: Any,
    *,
    request_type: str,
    session_id: Optional[str],
    inputs: Optional[dict] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Stamp LangSmith-flavor attributes on a parent OTel span."""
    if not ENABLED or span is None:
        return
    try:
        # `chain` is LangSmith's run-type for orchestration; voice turn /
        # chat request fits that.
        span.set_attribute("langsmith.span.kind", "chain")
        if session_id:
            # LangSmith threads view groups by this metadata key.
            span.set_attribute("langsmith.metadata.session_id", session_id)
        if _ENV:
            span.set_attribute("langsmith.metadata.env", _ENV)
        # Stamp arbitrary user metadata under langsmith.metadata.* so it
        # surfaces in LangSmith's metadata panel.
        for k, v in (metadata or {}).items():
            if v is not None:
                span.set_attribute(f"langsmith.metadata.{k}", str(v))
        if inputs:
            _stamp_inputs(span, inputs)
    except Exception as e:  # pragma: no cover
        logger.debug(f"[obs/langsmith] decorate_request_span: {e}")


def decorate_finish_span(
    span: Any,
    *,
    outputs: Optional[dict] = None,
) -> None:
    """Stamp outputs on a parent OTel span at finish_request time.

    `output.value` = JSON-encoded outputs dict — what LangSmith's Outputs
    panel reads. SDK doesn't dictate the shape; whatever the agent passes
    into finish_request(outputs={...}) gets serialized as-is.
    """
    if not ENABLED or span is None or not outputs:
        return
    try:
        span.set_attribute(
            "output.value", json.dumps(outputs, default=str)[:_INOUT_MAX_CHARS]
        )
    except Exception as e:  # pragma: no cover
        logger.debug(f"[obs/langsmith] decorate_finish_span: {e}")


def decorate_child_span(
    span: Any,
    *,
    kind: str = "tool",  # "tool" | "llm" | "retriever" | etc
    inputs: Optional[dict] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Stamp LangSmith-flavor attributes on a child OTel span."""
    if not ENABLED or span is None:
        return
    try:
        span.set_attribute("langsmith.span.kind", kind)
        if _ENV:
            span.set_attribute("langsmith.metadata.env", _ENV)
        for k, v in (metadata or {}).items():
            if v is not None:
                span.set_attribute(f"langsmith.metadata.{k}", str(v))
        if inputs:
            _stamp_inputs(span, inputs)
    except Exception as e:  # pragma: no cover
        logger.debug(f"[obs/langsmith] decorate_child_span: {e}")


def warn_audio_dropped(event: str) -> None:
    """One-time warning: OTel has no attachment primitive. v0.2 S3 path
    will re-enable audio playback."""
    global _audio_warning_fired
    if _audio_warning_fired:
        return
    _audio_warning_fired = True
    logger.warning(
        "[obs/langsmith] audio_wav passed to record_voice_event('%s') — "
        "OTel migration dropped LangSmith attachments. Audio will be "
        "playable again when the S3 audio path lands (plan §8.4).",
        event,
    )


# ─── Back-compat: wrap_openai ────────────────────────────────────
def wrap_openai(client: Any) -> Any:
    """Drop-in replacement for `langsmith.wrappers.wrap_openai`. Now uses
    OTel's openai_v2 auto-instrumentation, which emits OTel spans carrying
    `gen_ai.*` semantic-convention attributes — both Datadog LLM
    Observability and LangSmith recognise these."""
    return otel_init.instrument_openai_client(client)


# ─── Back-compat: function-level traceable API ───────────────────
# These predate the verb API. Kept working over OTel so legacy callers
# (none in propio_one anymore — verified) keep importing without breakage.

def _noop_decorator(*d_args, **d_kwargs):
    if len(d_args) == 1 and callable(d_args[0]) and not d_kwargs:
        return d_args[0]

    def wrapper(fn):
        return fn

    return wrapper


def traceable(name: Optional[str] = None, run_type: str = "chain", **metadata):
    """Back-compat: decorate a function so each call becomes an OTel span."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            tracer = otel_init.get_tracer()
            if tracer is None:
                return fn(*args, **kwargs)
            with tracer.start_as_current_span(name or fn.__name__) as span:
                span.set_attribute("langsmith.span.kind", run_type)
                for k, v in metadata.items():
                    if v is not None:
                        span.set_attribute(f"langsmith.metadata.{k}", str(v))
                return fn(*args, **kwargs)
        return wrapper
    return decorator


def traceable_llm(name: Optional[str] = None, **metadata):
    return traceable(name=name, run_type="llm", **metadata)


def traceable_stt(name: Optional[str] = None, **metadata):
    return traceable(name=name, run_type="tool", component="stt", **metadata)


def traceable_tts(name: Optional[str] = None, **metadata):
    return traceable(name=name, run_type="tool", component="tts", **metadata)


@contextlib.contextmanager
def turn_trace(session_id: str, transcript: str, **metadata):
    """Back-compat: open an OTel span for a turn. session_id maps to the
    LangSmith thread."""
    tracer = otel_init.get_tracer()
    if tracer is None:
        yield
        return
    with tracer.start_as_current_span("voice_turn") as span:
        span.set_attribute("langsmith.span.kind", "chain")
        span.set_attribute("langsmith.metadata.session_id", session_id)
        if transcript:
            span.set_attribute("langsmith.inputs.transcript", transcript[:2000])
        for k, v in metadata.items():
            if v is not None:
                span.set_attribute(f"langsmith.metadata.{k}", str(v))
        yield


@traceable_stt(name="stt_transcription")
def record_stt(
    transcript: str,
    provider: str,
    model: Optional[str],
    language: Optional[str],
    user_speech_ms: Optional[float],
    stt_finalize_ms: Optional[float],
    audio_wav: Optional[bytes] = None,
) -> dict:
    if audio_wav:
        warn_audio_dropped("stt_transcription")
    return {
        "transcript": transcript,
        "provider": provider,
        "model": model,
        "language": language,
        "transcript_chars": len(transcript) if transcript else 0,
        "transcript_words": len(transcript.split()) if transcript else 0,
        "user_speech_ms": user_speech_ms,
        "stt_finalize_ms": stt_finalize_ms,
        "audio_bytes": len(audio_wav) if audio_wav else None,
    }


@traceable_tts(name="tts_synthesis")
def record_tts(
    text: str,
    provider: str,
    model: Optional[str],
    voice: Optional[str],
    total_bytes: int,
    num_chunks: int,
    latency_ms: float,
    ttfb_ms: Optional[float] = None,
    audio_duration_ms: Optional[float] = None,
    audio_wav: Optional[bytes] = None,
) -> dict:
    if audio_wav:
        warn_audio_dropped("tts_synthesis")
    return {
        "text": text,
        "provider": provider,
        "model": model,
        "voice": voice,
        "text_chars": len(text),
        "total_bytes": total_bytes,
        "num_chunks": num_chunks,
        "ttfb_ms": ttfb_ms,
        "latency_ms": round(latency_ms, 2),
        "audio_duration_ms": audio_duration_ms,
    }


# Removed: open_request_run / emit_child_run / close_request_run helpers
# from the pre-OTel design. api.py now creates OTel spans directly via
# otel_init.get_tracer() and uses decorate_request_span / decorate_child_span
# above to add LangSmith conventions.
