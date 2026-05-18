"""Datadog APM exporter — emits parent/child spans via ddtrace.

Verb-layer helpers (open_request_span / emit_child_span / close_request_span)
mirror exporters/langsmith.py so api.py can fan-out a single verb call to both
backends in parallel.

Design choices (user-locked):
- **No auto-patch**: ddtrace.patch_all() is not called. Only spans emitted via
  the SDK's verb layer show up in Datadog. Avoids double-tracing OpenAI
  (LangSmith owns LLM traces).
- **DD Agent transport**: ddtrace ships spans to a Datadog Agent at
  localhost:8126 (override via DD_TRACE_AGENT_URL env var). Without an agent,
  ddtrace buffers + drops silently — no errors propagated.
- **ENABLED is checked at call time** so init_agent() can flip it on after
  module import (same robustness pattern as the LangSmith exporter).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─── Module state ────────────────────────────────────────────────
ENABLED: bool = False
_tracer: Optional[Any] = None
_service: Optional[str] = None
_env: Optional[str] = None
_version: Optional[str] = None


def _try_import_ddtrace() -> bool:
    global _tracer
    try:
        from ddtrace import tracer as _trc

        _tracer = _trc
        return True
    except Exception as e:  # pragma: no cover — import failure path
        logger.warning(f"[obs/datadog] ddtrace import failed: {e}")
        return False


def configure(
    *,
    enabled: bool,
    api_key: str,
    site: str,
    service: str,
    env: str,
    version: Optional[str] = None,
    agent_url: Optional[str] = None,
) -> None:
    """Activate Datadog from explicit values (called by init_agent)."""
    global ENABLED, _service, _env, _version
    if not enabled or not api_key:
        ENABLED = False
        return
    if not _try_import_ddtrace():
        ENABLED = False
        return

    # ddtrace reads DD_* env vars at import / startup. Set what's not already set.
    os.environ.setdefault("DD_API_KEY", api_key)
    os.environ.setdefault("DD_SITE", site)
    os.environ.setdefault("DD_SERVICE", service)
    os.environ.setdefault("DD_ENV", env)
    if version:
        os.environ.setdefault("DD_VERSION", version)
    if agent_url:
        os.environ.setdefault("DD_TRACE_AGENT_URL", agent_url)

    # Patch the tracer config for the current process (env vars only bind at
    # ddtrace import; agent_url change after that needs explicit config).
    try:
        from ddtrace import config as dd_config

        dd_config.service = service
        dd_config.env = env
        if version:
            dd_config.version = version
    except Exception as e:  # pragma: no cover — defensive
        logger.warning(f"[obs/datadog] ddtrace config patch failed: {e}")

    _service = service
    _env = env
    _version = version
    ENABLED = True
    logger.info(
        f"[obs/datadog] enabled (service={service}, env={env}, site={site})"
    )


def _stringify(value: Any, max_len: int = 500) -> str:
    """Coerce to a Datadog-tag-safe string (DD tags are stringly typed)."""
    try:
        s = value if isinstance(value, str) else str(value)
    except Exception:
        return ""
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s


def _set_tags(span: Any, prefix: str, mapping: Optional[dict]) -> None:
    if not mapping:
        return
    for k, v in mapping.items():
        if v is None:
            continue
        try:
            span.set_tag(f"{prefix}.{k}" if prefix else k, _stringify(v))
        except Exception as e:  # pragma: no cover
            logger.debug(f"[obs/datadog] set_tag failed for {k}: {e}")


def open_request_span(
    *,
    name: str,
    inputs: Optional[dict] = None,
    metadata: Optional[dict] = None,
) -> Optional[Any]:
    """Open a DD APM parent span for a request. Returns the span (caller must
    call close_request_span on it) or None when DD is disabled."""
    if not ENABLED or _tracer is None:
        return None
    try:
        span = _tracer.trace(
            name=name,
            resource=name,
            service=_service,
            span_type="custom",
        )
        # Standard Datadog tags first (env, service, version are already on the
        # tracer config but DD-UI surfaces them better when also explicit).
        if _env:
            span.set_tag("env", _env)
        if _version:
            span.set_tag("version", _version)
        _set_tags(span, "", metadata)
        _set_tags(span, "input", inputs)
        return span
    except Exception as e:
        logger.warning(f"[obs/datadog] open_request_span failed: {e}")
        return None


def emit_child_span(
    parent_span: Any,
    *,
    name: str,
    inputs: Optional[dict] = None,
    outputs: Optional[dict] = None,
    error: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Emit a one-shot child span under parent_span (voice events, tool calls)."""
    if not ENABLED or _tracer is None or parent_span is None:
        return
    try:
        # start_span with explicit child_of so siblings don't nest wrong.
        child = _tracer.start_span(
            name=name,
            child_of=parent_span,
            resource=name,
            service=_service,
            span_type="custom",
            activate=False,  # don't change current_span context — we manage manually
        )
        _set_tags(child, "", metadata)
        _set_tags(child, "input", inputs)
        _set_tags(child, "output", outputs)
        if error:
            child.error = 1
            child.set_tag("error.message", _stringify(error))
        child.finish()
    except Exception as e:
        logger.warning(f"[obs/datadog] emit_child_span '{name}' failed: {e}")


def close_request_span(
    span: Any,
    *,
    outputs: Optional[dict] = None,
    error: Optional[str] = None,
) -> None:
    """Finalize the parent DD span. Idempotent."""
    if span is None:
        return
    try:
        _set_tags(span, "output", outputs)
        if error:
            span.error = 1
            span.set_tag("error.message", _stringify(error))
        span.finish()
    except Exception as e:
        logger.warning(f"[obs/datadog] close_request_span failed: {e}")
