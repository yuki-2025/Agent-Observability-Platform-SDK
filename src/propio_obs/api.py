"""Verb-level API — the public interface every agent should use.

The 6 verbs:
    init_agent, start_request, record_tool, record_quality,
    record_voice_event, finish_request

Plus helpers:
    wrap_llm_client, flush

In v0 these verbs are intentionally thin — they delegate most work to the
LangSmith function-level API. The verb shape is the contract; internals get
fleshed out in v0.1+ when the router + multi-backend dispatch lands.
"""
from __future__ import annotations

import atexit
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union

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
_session_tenant_cache: Dict[str, str] = {}  # session_id → tenant_id, for SDK propagation
_postgres_db = PostgresDBExporter()
_atexit_registered: bool = False


# ─── 1. init_agent ───────────────────────────────────────────────
def init_agent(config: Union[str, Path, dict]) -> None:
    """Load config, configure backends. Idempotent — call once at startup."""
    global _config, _initialized, _atexit_registered

    if _initialized:
        logger.warning("[obs] init_agent already called; ignoring second invocation")
        return

    _config = AgentConfig.load(config)

    # Configure LangSmith from YAML if enabled
    ls_cfg = _config.backends.langsmith
    if ls_cfg.enabled:
        api_key = _config.resolve_env(ls_cfg.api_key_env)
        ls.configure(
            enabled=True,
            api_key=api_key,
            project=ls_cfg.project,
            endpoint=ls_cfg.endpoint,
        )

    # Configure Datadog from config if enabled
    dd_cfg = _config.backends.datadog
    if dd_cfg.enabled:
        dd_api_key = _config.resolve_env(dd_cfg.api_key_env)
        dd.configure(
            enabled=True,
            api_key=dd_api_key,
            site=dd_cfg.site,
            service=dd_cfg.service or _config.agent.service,
            env=dd_cfg.env_tag or _config.agent.environment,  # type: ignore[arg-type]
            version=dd_cfg.version,
            agent_url=dd_cfg.agent_url,
        )

    # Configure Datadog Logs (independent of APM)
    dl_cfg = _config.backends.datadog_logs
    if dl_cfg.enabled:
        dl_api_key = _config.resolve_env(dl_cfg.api_key_env)
        dd_logs.configure(
            enabled=True,
            api_key=dl_api_key,
            site=dl_cfg.site,
            service=dl_cfg.service or _config.agent.service,
            env=dl_cfg.env_tag or _config.agent.environment,  # type: ignore[arg-type]
            version=dl_cfg.version,
            agent_id=_config.agent.agent_id,
            min_level=getattr(logging, dl_cfg.min_level.upper(), logging.DEBUG),
            exclude_loggers=dl_cfg.exclude_loggers,
            batch_size=dl_cfg.batch_size,
            flush_interval_seconds=dl_cfg.flush_interval_seconds,
        )

    # Postgres event-mirror stub — wires up env resolution; actual writes are in v0.1
    _postgres_db.setup(_config.backends.postgres_db)

    # Register atexit flush so long-running servers flush cleanly on shutdown
    if not _atexit_registered:
        atexit.register(flush)
        _atexit_registered = True

    _initialized = True
    logger.info(
        f"[obs] init_agent OK (agent_id={_config.agent.agent_id}, "
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
    """Open a new request — mints request_id, opens a parent LangSmith RunTree,
    and returns an opaque handle.

    The RunTree (when LangSmith enabled) serves as parent for any child runs
    emitted via record_voice_event / record_tool, and is also installed as the
    ambient tracing_context so wrap_llm_client-wrapped LLM calls nest under it.
    Closed by finish_request().
    """
    md = dict(metadata or {})

    # Session-scoped tenant_id propagation (see plan §15.5)
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
        user_id_hash=user_id,  # caller is responsible for hashing — never raw user_id
        inputs=dict(inputs or {}),
        metadata=md,
    )
    req._state["started_at"] = time.perf_counter()

    # Open the langsmith parent run + ambient tracing_context, if enabled.
    # Propagate the platform correlation keys (plan §9) onto every run so
    # dashboards can pivot by env / tenant / agent / service.
    base_metadata: Dict[str, Any] = {
        "request_id": request_id,
        "request_type": request_type,
        "session_id": session_id,
        "agent_id": resolved_agent_id,
        "tenant_id": tenant_id,
    }
    if _config is not None:
        agent_cfg = _config.agent
        base_metadata.update(
            environment=agent_cfg.environment,
            agent_type=agent_cfg.agent_type,
            modality=agent_cfg.modality,
            service=agent_cfg.service,
        )
        if agent_cfg.version:
            base_metadata["version"] = agent_cfg.version
    base_metadata.update(md)

    handles = ls.open_request_run(
        name="agent.request",
        inputs=req.inputs,
        metadata=base_metadata,
    )
    if handles is not None:
        rt, cm = handles
        req._state["run_tree"] = rt
        req._state["tracing_cm"] = cm

    # Open the Datadog APM span. Independent of LangSmith — both, either, or
    # neither may be enabled.
    dd_span = dd.open_request_span(
        name="agent.request",
        inputs=req.inputs,
        metadata=base_metadata,
    )
    if dd_span is not None:
        req._state["dd_span"] = dd_span

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
    """Record a tool invocation — emits a child run under both the LangSmith
    parent RunTree and the Datadog parent span when present."""
    parent_rt = request._state.get("run_tree")
    parent_dd = request._state.get("dd_span")
    outputs_dict = {"output": output} if output is not None else None

    if ls.ENABLED and parent_rt is not None:
        ls.emit_child_run(
            parent_rt,
            name=f"tool.{name}",
            run_type="tool",
            inputs=input or {},
            outputs=outputs_dict,
            error=error,
            metadata=metadata,
        )
    elif ls.ENABLED:
        # Fallback: no parent RunTree → standalone traceable (back-compat).
        @ls.traceable(name=f"tool.{name}", run_type="tool", **(metadata or {}))
        def _emit(input: Any) -> Any:
            if error:
                raise RuntimeError(error)
            return output

        try:
            _emit(input)
        except RuntimeError:
            pass

    if dd.ENABLED and parent_dd is not None:
        dd.emit_child_span(
            parent_dd,
            name=f"tool.{name}",
            inputs=input or {},
            outputs=outputs_dict,
            error=error,
            metadata=metadata,
        )


# ─── 4. record_quality ───────────────────────────────────────────
def record_quality(
    request: Request,
    metric: str,
    value: Optional[Union[float, bool]] = None,
    *,
    comment: Optional[str] = None,
) -> None:
    """Record a product quality metric.

    In v0 we accept the value but only log it — LangSmith feedback API
    integration lands in v0.1. The primary path is LangSmith scheduled
    evaluators (see plan §15.3); record_quality() is for fast-path
    deterministic checks.
    """
    if _config and metric not in _config.quality_metrics and _config.quality_metrics:
        logger.warning(
            f"[obs] record_quality: metric '{metric}' not in config.quality_metrics "
            f"({_config.quality_metrics})"
        )
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
    """Record a voice event (asr_finalized / tts_finished / barge_in / ...).

    Emits a child run under both the LangSmith parent RunTree and the Datadog
    parent span when present. Audio is attached only to the LangSmith run
    (Datadog doesn't store binary attachments).
    """
    parent_rt = request._state.get("run_tree")
    parent_dd = request._state.get("dd_span")

    inputs: Dict[str, Any] = {"event": event}
    if metrics:
        inputs["metrics"] = metrics
    if metadata:
        inputs.update(metadata)

    audio_bytes = len(audio_wav) if audio_wav else None
    outputs = {"event": event, "audio_bytes": audio_bytes}

    if ls.ENABLED and parent_rt is not None:
        ls.emit_child_run(
            parent_rt,
            name=f"voice.{event}",
            run_type="tool",
            inputs=inputs,
            outputs=outputs,
            audio_wav=audio_wav,
            audio_name=f"audio_{event}",
            metadata={"component": "voice", **(metadata or {})},
        )
    elif ls.ENABLED:
        # Fallback: standalone child via @traceable + ambient context.
        md = dict(metadata or {})
        if metrics:
            md["metrics"] = metrics

        @ls.traceable(name=f"voice.{event}", run_type="tool", component="voice", **md)
        def _emit() -> dict:
            if audio_wav:
                ls._attach_audio(f"audio_{event}", audio_wav, "audio/wav")
            return outputs

        _emit()

    if dd.ENABLED and parent_dd is not None:
        dd.emit_child_span(
            parent_dd,
            name=f"voice.{event}",
            inputs=inputs,
            outputs=outputs,
            metadata={"component": "voice", **(metadata or {})},
        )


# ─── 6. finish_request ───────────────────────────────────────────
def finish_request(
    request: Request,
    *,
    status: str = "success",
    outputs: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    """Close the request — finalizes the LangSmith RunTree + ambient
    tracing_context, finalizes the Datadog APM span, and logs a summary.
    Idempotent: safe to call from multiple except branches.

    `status` is stamped as an output tag (`status=success|interrupted|error|…`)
    so both LangSmith and Datadog can group by it without using the error
    flag. `error` should only be set for true exceptions — interrupted/barge-in
    is a normal product outcome, not an error.
    """
    final_outputs: Dict[str, Any] = dict(outputs or {})
    final_outputs.setdefault("status", status)

    rt = request._state.pop("run_tree", None)
    cm = request._state.pop("tracing_cm", None)
    if rt is not None or cm is not None:
        ls.close_request_run(rt, cm, outputs=final_outputs, error=error)

    dd_span = request._state.pop("dd_span", None)
    if dd_span is not None:
        dd.close_request_span(dd_span, outputs=final_outputs, error=error)

    duration_hint = request._state.get("started_at")
    duration_ms = (
        round((time.perf_counter() - duration_hint) * 1000, 1) if duration_hint else None
    )
    logger.info(
        f"[obs] finish_request request_id={request.request_id} status={status} "
        f"duration_ms={duration_ms} error={error!r}"
    )


# ─── Helpers ─────────────────────────────────────────────────────
def wrap_llm_client(client: Any) -> Any:
    """Wrap an OpenAI / AsyncOpenAI client for auto-tracing.

    Identical to the function-level wrap_openai — kept under a verb-style name
    so the verb-only API is self-contained.
    """
    return ls.wrap_openai(client)


def flush(timeout_ms: int = 5000) -> None:
    """Flush pending exports. Called by atexit hook + explicitly for short-lived agents."""
    # LangSmith client flushes on its own; Datadog APM flushes via ddtrace's
    # own atexit. Only the Logs handler needs explicit flushing.
    dd_logs.flush(timeout_ms=timeout_ms)
    _postgres_db.shutdown(timeout_ms=timeout_ms)
