"""Postgres events-mirror exporter — stub for v0.

This is the SDK's catch-all Postgres backend: a relational store where every
agent's request/event summary rows can be mirrored, indexed, and joined for
the existing internal monitor UI and cross-agent analytics. Enabled by
default — every agent gets it unless explicitly opted out.

In v0 the propio voice agent's `monitor_service.broadcast()` writes events to
the shared Postgres (sessions/logs) and fires `pg_notify('monitor_events', ...)`;
the observability_platform service consumes that stream. This module is a
placeholder so the SDK schema matches what v0.1 will wire up.

Enable in `observability.yml` (or inline init_agent dict) as:

    backends:
      postgres_db:
        enabled: true        # already the default
        # url_env unset → SDK picks POSTGRES_DB_URL_<environment> from
        # propio_obs.platform_defaults.POSTGRES_DB_URL_ENV_BY_ENV.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class PostgresDBExporter:
    name = "postgres_db"

    def __init__(self) -> None:
        self.enabled = False
        self.url = ""

    def setup(self, cfg: Any) -> None:
        if not getattr(cfg, "enabled", False):
            return
        url_env = getattr(cfg, "url_env", None)
        if not url_env:
            logger.warning(
                "[obs/postgres_db] url_env unresolved; exporter inert. "
                "(Did agent.environment fail to map onto "
                "platform_defaults.POSTGRES_DB_URL_ENV_BY_ENV?)"
            )
            return
        self.url = os.environ.get(url_env, "")
        if not self.url:
            logger.warning(
                f"[obs/postgres_db] {url_env} not set; exporter will be inert"
            )
            return
        self.enabled = True
        logger.info(
            f"[obs/postgres_db] configured (env-var={url_env}; v0 writes still "
            "handled by monitor_service)"
        )

    def shutdown(self, timeout_ms: int = 5000) -> None:
        pass
