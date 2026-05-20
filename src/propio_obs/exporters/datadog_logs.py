"""Datadog Logs integration — collapsed under OTel migration.

Before: this module owned a custom logging.Handler that batched Python
LogRecords + POSTed to Datadog's HTTPS log intake API.

After: `propio_obs.otel_init` attaches an OTel `LoggingHandler` to the root
logger. Every Python `logger.info(...)` call becomes an OTel LogRecord →
OTLP HTTP → Collector → Datadog `datadog` exporter (same exporter as APM
traces) → Datadog Logs.

Trace correlation (log line ↔ APM trace pivot in DD UI) is now automatic:
OTel injects current span_id/trace_id into every LogRecord.

This module is now a thin config carrier for compatibility with init_agent.
"""
from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


ENABLED: bool = False


def configure(
    *,
    enabled: bool,
    api_key: str = "",
    site: str = "datadoghq.com",
    service: Optional[str] = None,
    env: Optional[str] = None,
    version: Optional[str] = None,
    agent_id: Optional[str] = None,
    min_level: int = logging.DEBUG,
    exclude_loggers: Optional[List[str]] = None,
    batch_size: int = 10,
    flush_interval_seconds: float = 5.0,
) -> None:
    """Record that Datadog Logs is enabled. Actual log shipping is handled
    by the OTel LoggingHandler attached in `otel_init.setup()` — we don't
    own a separate Handler anymore."""
    global ENABLED
    if not enabled:
        ENABLED = False
        return
    ENABLED = True
    logger.info(
        f"[obs/datadog_logs] enabled (routed via OTel Collector; "
        f"min_level={logging.getLevelName(min_level)})"
    )


def flush(timeout_ms: int = 5000) -> None:
    """No-op — flush is handled by the OTel BatchLogRecordProcessor in
    otel_init. Kept for api.py back-compat."""
    pass


def shutdown() -> None:
    """No-op — handled by otel_init.shutdown()."""
    pass
