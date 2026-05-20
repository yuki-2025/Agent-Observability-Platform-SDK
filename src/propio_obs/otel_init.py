"""Central OpenTelemetry SDK setup for propio-obs-sdk.

Single source of truth for OTel TracerProvider + LoggerProvider. All exporter
modules (langsmith, datadog, datadog_logs) share the tracer/logger from here
and only contribute attribute conventions on the spans/logs they care about.

Wire format: OTel emits spans/logs via OTLP HTTP to a Collector (configured at
`config.otel.collector_endpoint`, default `http://localhost:4318`). The Collector
then fan-outs to Datadog and LangSmith.

Failure isolation: if OTel setup fails (e.g., Collector unreachable on first
init), `ENABLED` stays False and all verb-layer calls become no-ops. The agent
process is unaffected.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─── Module state ────────────────────────────────────────────────
ENABLED: bool = False
_tracer_provider: Optional[Any] = None
_logger_provider: Optional[Any] = None
_tracer: Optional[Any] = None
_log_handler: Optional[logging.Handler] = None
_collector_endpoint: Optional[str] = None


# Lazy imports — avoid importing OTel at module load (keeps SDK import-fast
# when OTel deps aren't installed for some reason).
_OTLPSpanExporter: Optional[Any] = None
_OTLPLogExporter: Optional[Any] = None
_BatchSpanProcessor: Optional[Any] = None
_BatchLogRecordProcessor: Optional[Any] = None
_TracerProvider: Optional[Any] = None
_LoggerProvider: Optional[Any] = None
_Resource: Optional[Any] = None
_trace_set_provider: Optional[Any] = None
_LoggingHandler: Optional[Any] = None


def _try_import_otel() -> bool:
    """Best-effort import of the OTel primitives we need."""
    global _OTLPSpanExporter, _OTLPLogExporter
    global _BatchSpanProcessor, _BatchLogRecordProcessor
    global _TracerProvider, _LoggerProvider, _Resource
    global _trace_set_provider, _LoggingHandler
    try:
        from opentelemetry import trace as _trace
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as _SpanExp,
        )
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter as _LogExp,
        )
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        _OTLPSpanExporter = _SpanExp
        _OTLPLogExporter = _LogExp
        _BatchSpanProcessor = BatchSpanProcessor
        _BatchLogRecordProcessor = BatchLogRecordProcessor
        _TracerProvider = TracerProvider
        _LoggerProvider = LoggerProvider
        _Resource = Resource
        _trace_set_provider = _trace.set_tracer_provider
        _LoggingHandler = LoggingHandler
        # Also import _trace.get_tracer through trace.get_tracer when needed
        return True
    except Exception as e:  # pragma: no cover
        logger.warning(f"[obs/otel] import failed: {e}")
        return False


def setup(
    *,
    collector_endpoint: str,
    service_name: str,
    environment: str,
    agent_id: str,
    agent_type: str,
    modality: str,
    version: Optional[str] = None,
    extra_resource_attrs: Optional[dict] = None,
) -> None:
    """One-shot SDK init. Called from init_agent.

    Builds a shared TracerProvider + LoggerProvider with resource attributes
    that all OTel signals carry. Both are sent via OTLP HTTP to the Collector
    at `collector_endpoint`. Idempotent: second call logs a warning + no-ops.
    """
    global ENABLED, _tracer_provider, _logger_provider, _tracer, _log_handler
    global _collector_endpoint

    if ENABLED:
        logger.warning("[obs/otel] setup already called; ignoring")
        return

    if not _try_import_otel():
        return

    _collector_endpoint = collector_endpoint.rstrip("/")

    # Resource attributes — every signal carries these. DD auto-maps
    # service.name / deployment.environment / service.version onto its
    # service / env / version tags. agent.* are custom but show up as span
    # tags too.
    attrs = {
        "service.name": service_name,
        "deployment.environment": environment,
        "agent.id": agent_id,
        "agent.type": agent_type,
        "agent.modality": modality,
    }
    if version:
        attrs["service.version"] = version
    if extra_resource_attrs:
        attrs.update(extra_resource_attrs)

    resource = _Resource.create(attrs)

    # ── TracerProvider ──
    try:
        _tracer_provider = _TracerProvider(resource=resource)
        span_exporter = _OTLPSpanExporter(endpoint=f"{_collector_endpoint}/v1/traces")
        _tracer_provider.add_span_processor(_BatchSpanProcessor(span_exporter))
        _trace_set_provider(_tracer_provider)

        from opentelemetry import trace as _trace
        _tracer = _trace.get_tracer("propio_obs")
    except Exception as e:
        logger.warning(f"[obs/otel] tracer setup failed: {e}")
        return

    # ── LoggerProvider ──
    try:
        _logger_provider = _LoggerProvider(resource=resource)
        log_exporter = _OTLPLogExporter(endpoint=f"{_collector_endpoint}/v1/logs")
        _logger_provider.add_log_record_processor(_BatchLogRecordProcessor(log_exporter))

        from opentelemetry._logs import set_logger_provider
        set_logger_provider(_logger_provider)

        # Attach a Handler to Python's root logger so logger.info() flows to OTel.
        _log_handler = _LoggingHandler(
            level=logging.DEBUG, logger_provider=_logger_provider
        )
        logging.getLogger().addHandler(_log_handler)
    except Exception as e:
        logger.warning(f"[obs/otel] logger setup failed: {e}")

    ENABLED = True
    logger.info(
        f"[obs/otel] enabled (collector={_collector_endpoint}, "
        f"service={service_name}, env={environment}, agent={agent_id})"
    )


def get_tracer() -> Optional[Any]:
    """Return the configured OTel tracer, or None if OTel disabled."""
    return _tracer


def shutdown(timeout_ms: int = 5000) -> None:
    """Flush + shutdown both providers. Idempotent."""
    global ENABLED, _tracer_provider, _logger_provider, _tracer, _log_handler
    if _log_handler is not None:
        try:
            logging.getLogger().removeHandler(_log_handler)
        except Exception:  # pragma: no cover
            pass
        _log_handler = None
    if _tracer_provider is not None:
        try:
            _tracer_provider.shutdown()  # flushes
        except Exception as e:  # pragma: no cover
            logger.debug(f"[obs/otel] tracer shutdown: {e}")
        _tracer_provider = None
    if _logger_provider is not None:
        try:
            _logger_provider.shutdown()
        except Exception as e:  # pragma: no cover
            logger.debug(f"[obs/otel] logger shutdown: {e}")
        _logger_provider = None
    _tracer = None
    ENABLED = False


def instrument_openai_client(client: Any) -> Any:
    """Replace `langsmith.wrappers.wrap_openai`. Calls OpenAI auto-instrumentation
    so every chat.completions.create / responses.create becomes an OTel span
    carrying `gen_ai.*` semantic conventions (recognized by both Datadog LLM
    Observability and LangSmith).

    Idempotent — patching the same client twice is fine; the instrumentor
    short-circuits.
    """
    if not ENABLED:
        return client
    try:
        from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor

        OpenAIInstrumentor().instrument()
    except Exception as e:
        logger.warning(f"[obs/otel] OpenAI auto-instrumentation failed: {e}")
    return client
