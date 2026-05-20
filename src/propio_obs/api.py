"""Verb-level API — the public interface every agent should use.

The 6 verbs:
    init_agent, start_request, record_tool, record_quality,
    record_voice_event, finish_request

Plus session / event-mirror verbs (PG-backed):
    start_session, end_session, broadcast_event

Plus helpers:
    wrap_llm_client, flush

OTel migration note: all "trace-shaped" verbs (start_request /
record_voice_event / record_tool / finish_request) now create OTel spans
via the shared tracer in `propio_obs.otel_init`. Spans ship via OTLP HTTP
to a Collector, which fan-outs to LangSmith + Datadog. No more per-exporter
SDK calls — one wire path.

The Postgres event-mirror verbs (start_session / broadcast_event /
end_session) are unchanged — they write business events to a Postgres
DB via asyncpg, not via OTel signals.
"""
from __future__ import annotations

import atexit
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union

from . import otel_init
from .audio import pcm16_to_wav
from .config import AgentConfig
from .exporters import datadog as dd
from .exporters import datadog_logs as dd_logs
from .exporters import langsmith as ls
from .exporters.postgres_db import PostgresDBExporter
from .ids import new_request_id, new_turn_id
from .request import Request

logger = logging.getLogger(__name__)


# ─── Module state ────────────────────────────────────────────────
_config: Optional[AgentConfig] = None
_initialized: bool = False
_session_tenant_cache: Dict[str, str] = {}
_postgres_db = PostgresDBExporter()
_atexit_registered: bool = False


# ─── 1. init_agent ───────────────────────────────────────────────
def init_agent(config: Union[str, Path, dict]) -> None:
    """Load config, set up OTel + backend modules. Idempotent."""
    global _config, _initialized, _atexit_registered

    if _initialized:
        logger.warning("[obs] init_agent already called; ignoring second invocation")
        return

    _config = AgentConfig.load(config)

    # 1. Bring up OTel (single shared tracer + logger). Every "trace" verb
    #    below resolves to a span on this tracer.
    otel_init.setup(
        collector_endpoint=_config.otel.collector_endpoint,
        service_name=_config.agent.service,
        environment=_config.agent.environment,  # type: ignore[arg-type]
        agent_id=_config.agent.agent_id,
        agent_type=_config.agent.agent_type,
        modality=_config.agent.modality,
        version=_config.agent.version,
    )

    # 2. Register intent for each enabled backend. The Collector owns the
    #    actual ship paths; these flags just gate which attribute
    #    conventions the verb layer applies.
    ls_cfg = _config.backends.langsmith
    if ls_cfg.enabled:
        ls.configure(
            enabled=True,
            project=ls_cfg.project,
            env=_config.agent.environment,  # type: ignore[arg-type]
        )

    dd_cfg = _config.backends.datadog
    if dd_cfg.enabled:
        dd.configure(
            enabled=True,
            service=dd_cfg.service or _config.agent.service,
            env=dd_cfg.env_tag or _config.agent.environment,  # type: ignore[arg-type]
            version=dd_cfg.version,
        )

    dl_cfg = _config.backends.datadog_logs
    if dl_cfg.enabled:
        dd_logs.configure(
            enabled=True,
            service=dl_cfg.service or _config.agent.service,
            env=dl_cfg.env_tag or _config.agent.environment,  # type: ignore[arg-type]
            version=dl_cfg.version,
            min_level=getattr(logging, dl_cfg.min_level.upper(), logging.DEBUG),
            exclude_loggers=dl_cfg.exclude_loggers,
        )

    # 3. Postgres event-mirror (asyncpg, NOT OTel — domain DB).
    _postgres_db.setup(_config.backends.postgres_db)

    if not _atexit_registered:
        atexit.register(flush)
        _atexit_registered = True

    _initialized = True
    logger.info(
        f"[obs] init_agent OK (agent_id={_config.agent.agent_id}, "
        f"otel={'on' if otel_init.ENABLED else 'off'}, "
        f"langsmith={'on' if ls.ENABLED else 'off'}, "
        f"datadog={'on' if dd.ENABLED else 'off'}, "
        f"datadog_logs={'on' if dd_logs.ENABLED else 'off'})"
    )


# ─── 2. start_request ────────────────────────────────────────────
def start_request(
    request_type: str,
    *,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    inputs: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Request:
    """Open a new request — mints request_id, opens a parent OTel span,
    activates it as the current span so any wrapped LLM call or child verb
    nests underneath. Closed by finish_request().
    """
    md = dict(metadata or {})

    # Session-scoped tenant_id propagation (plan §15.5).
    tenant_id = md.pop("tenant_id", None) or md.pop("customer_id", None)
    if tenant_id and session_id:
        _session_tenant_cache[session_id] = tenant_id
    elif session_id and session_id in _session_tenant_cache:
        tenant_id = _session_tenant_cache[session_id]

    resolved_agent_id = agent_id or (_config.agent.agent_id if _config else None)
    request_id = new_request_id()

    req = Request(
        request_id=request_id,
        request_type=request_type,
        agent_id=resolved_agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        user_id_hash=user_id,
        inputs=dict(inputs or {}),
        metadata=md,
    )
    req._state["started_at"] = time.perf_counter()

    tracer = otel_init.get_tracer()
    if tracer is None:
        return req

    # Open the parent span. Use a server-kind span so DD shows it correctly
    # in service map (incoming request).
    try:
        from opentelemetry import context, trace
        from opentelemetry.trace import SpanKind

        span = tracer.start_span("agent.request", kind=SpanKind.SERVER)
        # Standard correlation keys — every span carries these.
        span.set_attribute("request.id", request_id)
        span.set_attribute("request.type", request_type)
        if session_id:
            span.set_attribute("session.id", session_id)
        if tenant_id:
            span.set_attribute("tenant.id", tenant_id)
        if resolved_agent_id:
            span.set_attribute("agent.id", resolved_agent_id)

        # Backend-specific attribute decoration.
        ls.decorate_request_span(
            span,
            request_type=request_type,
            session_id=session_id,
            inputs=req.inputs,
            metadata=md,
        )
        dd.decorate_request_span(span, request_type=request_type, inputs=req.inputs)

        # Activate this span as the current span so child verbs and
        # auto-instrumented LLM calls nest under it.
        ctx = trace.set_span_in_context(span)
        token = context.attach(ctx)

        req._state["span"] = span
        req._state["context_token"] = token
    except Exception as e:
        logger.warning(f"[obs] start_request OTel span failed: {e}")

    return req


# ─── 3. record_tool ──────────────────────────────────────────────
def record_tool(
    request: Request,
    name: str,
    *,
    input: Optional[Dict[str, Any]] = None,
    output: Optional[Any] = None,
    error: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit a child OTel span for a tool invocation."""
    tracer = otel_init.get_tracer()
    if tracer is None:
        return
    try:
        with tracer.start_as_current_span(f"tool.{name}") as span:
            ls.decorate_child_span(
                span, kind="tool", inputs=input, metadata=metadata
            )
            if output is not None:
                span.set_attribute("tool.output", str(output)[:2000])
            if error:
                span.set_attribute("error", True)
                span.set_attribute("error.message", error[:500])
    except Exception as e:
        logger.warning(f"[obs] record_tool '{name}' failed: {e}")


# ─── 4. record_quality ───────────────────────────────────────────
def record_quality(
    request: Request,
    metric: str,
    value: Optional[Union[float, bool]] = None,
    *,
    comment: Optional[str] = None,
) -> None:
    """Stamp a product quality metric on the current request span.

    LangSmith scheduled evaluators still run on trace I/O (plan §15.3).
    This verb is the fast-path for deterministic checks (e.g.
    "tool_returned_data": True/False).
    """
    if _config and metric not in _config.quality_metrics and _config.quality_metrics:
        logger.warning(
            f"[obs] record_quality: metric '{metric}' not in config.quality_metrics "
            f"({_config.quality_metrics})"
        )
    span = request._state.get("span")
    if span is not None:
        try:
            span.set_attribute(f"quality.{metric}", str(value) if value is not None else "")
            if comment:
                span.set_attribute(f"quality.{metric}.comment", comment[:500])
        except Exception:  # pragma: no cover
            pass
    logger.info(
        f"[obs] quality {metric}={value} request_id={request.request_id} "
        f"comment={comment!r}"
    )


# ─── 5. record_voice_event ───────────────────────────────────────
def record_voice_event(
    request: Request,
    event: str,
    *,
    metrics: Optional[Dict[str, float]] = None,
    audio_wav: Optional[bytes] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit a child OTel span for a voice event (asr_finalized / tts_finished /
    barge_in / ...).

    `audio_wav` is accepted for API compatibility but ignored — OTel has no
    binary attachment primitive. Audio playback returns in v0.2 via the S3
    audio path (plan §8.4). A one-time warning fires per process.
    """
    tracer = otel_init.get_tracer()
    if tracer is None:
        return

    if audio_wav:
        ls.warn_audio_dropped(event)

    inputs: Dict[str, Any] = {"event": event}
    if metrics:
        inputs["metrics"] = metrics
    if metadata:
        inputs.update(metadata)

    try:
        with tracer.start_as_current_span(f"voice.{event}") as span:
            span.set_attribute("voice.event", event)
            if metrics:
                for k, v in metrics.items():
                    if v is not None:
                        span.set_attribute(f"voice.metrics.{k}", v)
            ls.decorate_child_span(
                span,
                kind="tool",
                inputs=inputs,
                metadata={"component": "voice", **(metadata or {})},
            )
            if audio_wav:
                # Don't ship the bytes — just the length so dashboards can
                # filter "events with audio" without the payload.
                span.set_attribute("voice.audio_bytes", len(audio_wav))
    except Exception as e:
        logger.warning(f"[obs] record_voice_event '{event}' failed: {e}")


# ─── 6. finish_request ───────────────────────────────────────────
def finish_request(
    request: Request,
    *,
    status: str = "success",
    outputs: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    """Close the request — finalizes the parent OTel span + logs a summary.
    Idempotent: safe to call from multiple except branches.

    `status` is stamped as a span attribute (`status=success|interrupted|
    error|...`) so both LangSmith and Datadog can group by it. `error`
    should only be set for true exceptions — interrupted/barge-in is a
    normal product outcome, not an error.
    """
    span = request._state.pop("span", None)
    token = request._state.pop("context_token", None)

    if span is not None:
        try:
            span.set_attribute("status", status)
            for k, v in (outputs or {}).items():
                if v is not None:
                    span.set_attribute(f"output.{k}", str(v)[:2000])
            # LangSmith Outputs panel reads `output.value` (JSON). Per-key
            # `output.{k}` above stays for Datadog tag-filter use.
            ls.decorate_finish_span(span, outputs=outputs)
            if error:
                from opentelemetry.trace import Status, StatusCode

                span.set_attribute("error", True)
                span.set_attribute("error.message", error[:500])
                span.set_status(Status(StatusCode.ERROR, error[:200]))
            span.end()
        except Exception as e:  # pragma: no cover
            logger.warning(f"[obs] finish_request span end failed: {e}")

    if token is not None:
        try:
            from opentelemetry import context
            context.detach(token)
        except Exception:  # pragma: no cover
            pass

    duration_hint = request._state.get("started_at")
    duration_ms = (
        round((time.perf_counter() - duration_hint) * 1000, 1) if duration_hint else None
    )
    logger.info(
        f"[obs] finish_request request_id={request.request_id} status={status} "
        f"duration_ms={duration_ms} error={error!r}"
    )


# ─── 7. Session lifecycle + event mirror (PG-backed, NOT OTel) ───
async def start_session(
    session_id: str,
    *,
    config: Optional[Dict[str, Any]] = None,
    env: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> None:
    """Register a session in Postgres + start heartbeat. PG path; unchanged
    by OTel migration."""
    resolved_agent_id = agent_id or (_config.agent.agent_id if _config else None)
    resolved_env = env or (_config.agent.environment if _config else None)
    if not resolved_env:
        raise RuntimeError(
            "start_session: env unresolved. Either call init_agent() first "
            "(which validates agent.environment) or pass env= explicitly."
        )
    await _postgres_db.start_session(
        session_id, env=resolved_env, config=config, agent_id=resolved_agent_id
    )


async def end_session(session_id: str) -> None:
    await _postgres_db.end_session(session_id)


async def broadcast_event(
    event: Dict[str, Any],
    *,
    session_id: str,
    agent_id: Optional[str] = None,
) -> None:
    """Fire-and-forget INSERT of a pipeline event into Postgres + pg_notify."""
    resolved_agent_id = agent_id or (_config.agent.agent_id if _config else None)
    await _postgres_db.broadcast_event(
        event, session_id=session_id, agent_id=resolved_agent_id
    )


# ─── Helpers ─────────────────────────────────────────────────────
def wrap_llm_client(client: Any) -> Any:
    """Wrap an OpenAI / AsyncOpenAI client so every call auto-emits an
    OTel span carrying gen_ai.* semantic-convention attributes.

    Under OTel migration this delegates to OpenAI auto-instrumentation
    rather than langsmith.wrappers.wrap_openai.
    """
    return otel_init.instrument_openai_client(client)


def flush(timeout_ms: int = 5000) -> None:
    """Flush pending exports. Called by atexit + explicitly for short-lived
    agents."""
    try:
        otel_init.shutdown(timeout_ms=timeout_ms)
    except Exception:  # pragma: no cover
        pass
    _postgres_db.shutdown(timeout_ms=timeout_ms)
