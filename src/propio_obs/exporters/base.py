"""Exporter Protocol — common shape every backend adapter implements.

In v0 this is intentionally minimal. v0.1 will expand with explicit lifecycle
methods (setup / shutdown) when the router becomes async.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Exporter(Protocol):
    """One adapter per backend (LangSmith / Datadog / Propio DB / S3 / ...)."""

    name: str

    def setup(self, cfg: Any) -> None:
        """Read backend config + initialize client. Called once from init_agent()."""
        ...

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Flush pending events. Called from atexit hook + obs.flush()."""
        ...
