"""LangSmith exporter — direct port of backend/app/services/tracing.py.

Function-level API kept identical so existing callers (propio voice agent's
record_stt / record_tts / turn_trace / wrap_openai) work unchanged after the
backend swaps to importing from propio_obs.

When LangSmith is not enabled (no API key, or disabled in config), every
public symbol is a no-op — zero runtime overhead, zero import errors.
"""
from __future__ import annotations

import contextlib
import functools
import logging
import os
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ─── Module-level state ──────────────────────────────────────────
# ENABLED is computed lazily by configure() — defaults to env-var-driven
# behavior so legacy callers (no init_agent) still work.
ENABLED: bool = False
_PROJECT: Optional[str] = None

_langsmith_traceable: Optional[Callable] = None
_wrap_openai_impl: Optional[Callable] = None
_get_current_run_tree: Optional[Callable] = None
_Attachment: Optional[Any] = None
_tracing_context: Optional[Callable] = None
_RunTree: Optional[Any] = None


def _try_import_langsmith() -> bool:
    """Best-effort import of langsmith primitives. Returns True if all imports succeed."""
    global _langsmith_traceable, _wrap_openai_impl, _get_current_run_tree
    global _Attachment, _tracing_context, _RunTree
    try:
        from langsmith import (
            RunTree as _Rt,
            get_current_run_tree as _gcrt,
            traceable as _trc,
            tracing_context as _tc,
        )
        from langsmith.schemas import Attachment as _Att
        from langsmith.wrappers import wrap_openai as _wop

        _langsmith_traceable = _trc
        _get_current_run_tree = _gcrt
        _wrap_openai_impl = _wop
        _Attachment = _Att
        _tracing_context = _tc
        _RunTree = _Rt
        return True
    except Exception as e:  # pragma: no cover — import failure path
        logger.warning(f"[obs/langsmith] import failed, tracing disabled: {e}")
        return False


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("true", "1", "yes")


def configure(
    enabled: bool,
    api_key: str = "",
    project: str = "default",
    endpoint: str = "https://api.smith.langchain.com",
) -> None:
    """Activate LangSmith from explicit values (called by init_agent)."""
    global ENABLED, _PROJECT
    if not enabled or not api_key:
        ENABLED = False
        return
    # The langsmith SDK reads LANGCHAIN_* env vars.
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = api_key
    os.environ["LANGCHAIN_PROJECT"] = project
    os.environ["LANGCHAIN_ENDPOINT"] = endpoint
    if _try_import_langsmith():
        ENABLED = True
        _PROJECT = project
        logger.info(f"[obs/langsmith] enabled (project={project})")
    else:
        ENABLED = False


def _autoconfigure_from_env() -> None:
    """Fallback path: if init_agent() wasn't called, try plain env vars.

    Mirrors the original tracing.py behavior so legacy callers keep working.
    """
    if ENABLED:
        return
    # Two env name conventions supported:
    #   - LANGSMITH_*  (Propio's settings)
    #   - LANGCHAIN_*  (langsmith SDK native)
    if _env_truthy("LANGSMITH_TRACING") or _env_truthy("LANGCHAIN_TRACING_V2"):
        configure(
            enabled=True,
            api_key=os.environ.get("LANGSMITH_API_KEY")
            or os.environ.get("LANGCHAIN_API_KEY", ""),
            project=os.environ.get("LANGSMITH_PROJECT")
            or os.environ.get("LANGCHAIN_PROJECT", "default"),
            endpoint=os.environ.get("LANGSMITH_ENDPOINT")
            or os.environ.get(
                "LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com"
            ),
        )


# Run autoconfigure at import time so `from propio_obs import record_stt` works
# without explicit init_agent — preserves the function-level API contract.
_autoconfigure_from_env()


# ─── Audio attachment helper ─────────────────────────────────────
def _attach_audio(name: str, audio_bytes: Optional[bytes], mime: str = "audio/wav") -> None:
    """Attach audio to the currently active run, if a run is active."""
    if not ENABLED or not audio_bytes or _get_current_run_tree is None or _Attachment is None:
        return
    try:
        rt = _get_current_run_tree()
        if rt is None:
            return
        rt.attachments[name] = _Attachment(mime_type=mime, data=audio_bytes)
    except Exception as e:
        logger.warning(f"[obs/langsmith] attach_audio '{name}' failed: {e}")


# ─── Decorator factory ───────────────────────────────────────────
def _noop_decorator(*d_args, **d_kwargs):
    """Pass-through. Supports both @deco and @deco(...) forms."""
    if len(d_args) == 1 and callable(d_args[0]) and not d_kwargs:
        return d_args[0]

    def wrapper(fn):
        return fn

    return wrapper


def traceable(name: Optional[str] = None, run_type: str = "chain", **metadata):
    """Generic traceable decorator. ENABLED is checked at *call* time so the
    decorator survives init_agent() running after module-level decoration —
    the typical pattern for record_stt / record_tts.

    The langsmith-wrapped fn is built lazily on first traced call and cached
    in the closure so per-call overhead stays at ~one bool check + one call.

    run_type ∈ {chain, llm, tool, retriever, embedding, prompt, parser}.
    """

    def decorator(fn):
        _traced: Optional[Callable] = None

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            nonlocal _traced
            if not ENABLED or _langsmith_traceable is None:
                return fn(*args, **kwargs)
            if _traced is None:
                ls_kwargs: dict = {"run_type": run_type, "metadata": metadata}
                if name:
                    ls_kwargs["name"] = name
                _traced = _langsmith_traceable(**ls_kwargs)(fn)
            return _traced(*args, **kwargs)

        return wrapper

    return decorator


def traceable_llm(name: Optional[str] = None, **metadata):
    return traceable(name=name, run_type="llm", **metadata)


def traceable_stt(name: Optional[str] = None, **metadata):
    return traceable(name=name, run_type="tool", component="stt", **metadata)


def traceable_tts(name: Optional[str] = None, **metadata):
    return traceable(name=name, run_type="tool", component="tts", **metadata)


# ─── Turn trace context manager ──────────────────────────────────
@contextlib.contextmanager
def turn_trace(session_id: str, transcript: str, **metadata):
    """Group child runs under one parent 'voice_turn' run via tracing_context."""
    if not ENABLED or _tracing_context is None:
        yield
        return
    try:
        with _tracing_context(
            project_name=_PROJECT,
            metadata={"session_id": session_id, "transcript": transcript, **metadata},
        ):
            yield
    except Exception as e:
        logger.warning(f"[obs/langsmith] turn_trace fallback (no context manager): {e}")
        yield


# ─── Recorders ───────────────────────────────────────────────────
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
    """Log a completed STT transcription as a child of the current turn run."""
    _attach_audio("user_audio", audio_wav, "audio/wav")
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
    """Log a completed TTS synthesis as a child of the current turn run."""
    _attach_audio("agent_audio", audio_wav, "audio/wav")
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


# ─── OpenAI client wrapper ───────────────────────────────────────
def wrap_openai(client: Any) -> Any:
    """Wrap an OpenAI / AsyncOpenAI client so calls are auto-traced."""
    if not ENABLED or _wrap_openai_impl is None:
        return client
    try:
        return _wrap_openai_impl(client)
    except Exception as e:
        logger.warning(f"[obs/langsmith] wrap_openai failed, returning raw client: {e}")
        return client


# ─── Verb-layer RunTree helpers (Stage B) ────────────────────────
# These back the public verbs in api.py — start_request / record_voice_event /
# record_tool / finish_request — by managing a langsmith RunTree per request
# and an ambient tracing_context so wrapped LLM calls nest underneath.

def open_request_run(
    *,
    name: str,
    inputs: Optional[dict] = None,
    metadata: Optional[dict] = None,
) -> Optional[tuple]:
    """Open a langsmith parent RunTree for a request. Returns (rt, cm) or None.

    `cm` is the active tracing_context — caller must close it via
    close_request_run() when the request finishes.
    """
    if not ENABLED or _RunTree is None or _tracing_context is None:
        return None
    try:
        rt = _RunTree(
            name=name,
            run_type="chain",
            inputs=dict(inputs or {}),
            extra={"metadata": dict(metadata or {})},
            project_name=_PROJECT,
        )
        rt.post()
        cm = _tracing_context(parent=rt, project_name=_PROJECT)
        cm.__enter__()
        return rt, cm
    except Exception as e:
        logger.warning(f"[obs/langsmith] open_request_run failed: {e}")
        return None


def emit_child_run(
    parent_rt: Any,
    *,
    name: str,
    run_type: str = "tool",
    inputs: Optional[dict] = None,
    outputs: Optional[dict] = None,
    error: Optional[str] = None,
    audio_wav: Optional[bytes] = None,
    audio_name: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Emit a one-shot child run under parent_rt — used for voice events and
    tool calls where all data is available at emit time."""
    if not ENABLED or parent_rt is None:
        return
    try:
        child = parent_rt.create_child(
            name=name,
            run_type=run_type,
            inputs=dict(inputs or {}),
        )
        if metadata:
            try:
                child.add_metadata(dict(metadata))
            except Exception:
                pass
        if audio_wav and _Attachment is not None:
            child.attachments[audio_name or "audio"] = _Attachment(
                mime_type="audio/wav", data=audio_wav
            )
        if error:
            child.end(error=error)
        else:
            child.end(outputs=dict(outputs or {}))
        child.post()
        child.patch()
    except Exception as e:
        logger.warning(f"[obs/langsmith] emit_child_run '{name}' failed: {e}")


def close_request_run(
    rt: Any,
    cm: Any,
    *,
    outputs: Optional[dict] = None,
    error: Optional[str] = None,
) -> None:
    """Finalize the parent RunTree and exit its tracing_context. Idempotent."""
    if rt is not None:
        try:
            rt.end(outputs=dict(outputs or {}), error=error)
            rt.patch()
        except Exception as e:
            logger.warning(f"[obs/langsmith] close_request_run end/patch failed: {e}")
    if cm is not None:
        try:
            cm.__exit__(None, None, None)
        except Exception as e:  # pragma: no cover — defensive
            logger.warning(f"[obs/langsmith] tracing_context exit failed: {e}")
