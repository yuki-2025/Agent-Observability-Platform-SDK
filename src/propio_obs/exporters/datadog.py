"""Datadog APM integration — collapsed under OTel migration.

Before: this module owned ddtrace tracer setup + span emission via the
Datadog Trace Agent at `localhost:8126`. After OTel migration: spans are
emitted by the shared OTel tracer in `propio_obs.otel_init`, ship via OTLP
to the Collector, and the Collector's `datadog` exporter forwards to DD.

Datadog's standard mapping picks up:
- `service.name` → DD `service` tag (resource attr, set by otel_init)
- `deployment.environment` → DD `env` tag (resource attr)
- `service.version` → DD `version` tag (resource attr)
- `gen_ai.*` semantic conventions → DD LLM Observability (set by OpenAI auto-instrumentation)

Custom attrs (`agent.id`, `tenant.id`, `request.id`, `session.id`) are
populated on individual spans by api.py and show up as DD span tags.

So this module is now near-empty. Kept as a config carrier so `init_agent`
can record intent ("Datadog enabled? yes/no") for diagnostics.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─── Module state ────────────────────────────────────────────────
ENABLED: bool = False


def configure(
    *,
    enabled: bool,
    api_key: str = "",
    site: str = "datadoghq.com",
    service: Optional[str] = None,
    env: Optional[str] = None,
    version: Optional[str] = None,
    agent_url: Optional[str] = None,
) -> None:
    """Record that Datadog is enabled. Actual DD credentials + endpoint are
    configured at the OTel Collector layer (deploy/otel-collector-config.yaml);
    the agent process no longer talks to DD directly.

    Most parameters are kept for back-compat with the pre-OTel call signature
    but are informational.
    """
    global ENABLED
    if not enabled:
        ENABLED = False
        return
    ENABLED = True
    logger.info(
        f"[obs/datadog] enabled (routed via OTel Collector; "
        f"service={service or '<resource>'}, env={env or '<resource>'})"
    )


def decorate_request_span(
    span: Any,
    *,
    request_type: str,
    inputs: Optional[dict] = None,
    outputs: Optional[dict] = None,
) -> None:
    """Optional DD-specific span attributes. DD's exporter already picks up
    resource attrs + standard span fields; this is for any DD-only tag that
    isn't covered by resource."""
    if not ENABLED or span is None:
        return
    try:
        # Resource name pattern — DD displays this in service map.
        span.set_attribute("resource.name", request_type)
    except Exception as e:  # pragma: no cover
        logger.debug(f"[obs/datadog] decorate_request_span: {e}")
