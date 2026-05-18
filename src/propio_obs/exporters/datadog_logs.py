"""Datadog Logs exporter — ships Python logging records to DD Logs Intake API.

Independent of the APM exporter (`exporters/datadog.py`). Wired separately via
the `datadog_logs` backend in AgentConfig. APM and Logs are two different DD
features; you can run either, both, or neither.

Implementation notes:
- A `logging.Handler` subclass is attached to the root logger on configure().
- Records are batched and POSTed to `https://http-intake.logs.{site}/api/v2/logs`
  via the `datadog_api_client` library (async POST + exponential-backoff retry).
- `DD_LOGS_INJECTION=true` is set in the process env so ddtrace stamps
  `dd.trace_id` / `dd.span_id` onto every LogRecord — we propagate those into
  the HTTP log item's attributes so DD UI's log→trace pivot works.
- Excluded loggers (`ddtrace`, `urllib3`, `datadog`, `httpx` by default) are
  filtered at emit() time to avoid noise and to keep the log-shipping channel
  from observing itself.
- Failure isolation: emit() never raises; failed batches drop after retry.

Future OTel migration: replace DatadogAsyncHandler with
`opentelemetry.sdk._logs.LoggingHandler` + OTLP HTTP exporter. The
configure() signature and AgentConfig schema do not change.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from logging import LogRecord
from queue import Queue
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


# ─── Module state ────────────────────────────────────────────────
ENABLED: bool = False
_handler: Optional[logging.Handler] = None
_HTTPLogItem: Optional[Any] = None
_LogsApi: Optional[Any] = None
_ApiClient: Optional[Any] = None
_Configuration: Optional[Any] = None
_HTTPLog: Optional[Any] = None
_unset: Optional[Any] = None


def _try_import_datadog_api_client() -> bool:
    """Lazy import. Returns True if all imports succeed."""
    global _HTTPLogItem, _LogsApi, _ApiClient, _Configuration, _HTTPLog, _unset
    try:
        from datadog_api_client import ApiClient, Configuration
        from datadog_api_client.v2.api.logs_api import LogsApi
        from datadog_api_client.v2.model.http_log import HTTPLog
        from datadog_api_client.v2.model.http_log_item import HTTPLogItem, unset

        _ApiClient = ApiClient
        _Configuration = Configuration
        _LogsApi = LogsApi
        _HTTPLog = HTTPLog
        _HTTPLogItem = HTTPLogItem
        _unset = unset
        return True
    except Exception as e:  # pragma: no cover
        logger.warning(f"[obs/datadog_logs] datadog_api_client import failed: {e}")
        return False


class _DatadogAsyncHandler(logging.Handler):
    """Batched async logging.Handler that POSTs to DD Logs Intake API.

    Adapted from scheduling-agent/service/app/utils/datadog_handler.py;
    wrapped here as a private module detail so the public surface stays
    `configure()` / `flush()` / `shutdown()`.
    """

    def __init__(
        self,
        *,
        api_key: str,
        service: str,
        env: str,
        version: Optional[str] = None,
        site: str = "datadoghq.com",
        source: str = "python",
        agent_id: Optional[str] = None,
        batch_size: int = 10,
        flush_interval_seconds: float = 5.0,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        exclude_loggers: Optional[List[str]] = None,
        level: int = logging.DEBUG,
    ) -> None:
        super().__init__(level)
        self.api_key = api_key
        self.service = service
        self.env = env
        self.version = version
        self.source = source
        self.hostname = os.getenv("HOSTNAME", "unknown")
        self.agent_id = agent_id
        self.batch_size = batch_size
        self.flush_interval = flush_interval_seconds
        self.timeout = timeout_seconds
        self.max_retries = max_retries
        self.exclude_loggers = tuple(exclude_loggers or ())

        # DD API client
        config = _Configuration()
        config.api_key["apiKeyAuth"] = self.api_key
        config.server_variables["site"] = site
        self.api_client = _ApiClient(config)
        self.logs_api = _LogsApi(self.api_client)

        # Batch + queue
        self._log_batch: list = []
        self._lock: Optional[asyncio.Lock] = None
        self._flush_task: Optional[asyncio.Task] = None
        self._last_flush_time = time.time()
        self._closed = False

        # Background thread (for sync contexts — uvicorn before lifespan)
        self._background_thread: Optional[threading.Thread] = None
        self._background_loop: Optional[asyncio.AbstractEventLoop] = None
        self._log_queue: Queue = Queue(maxsize=1000)

        # Start the periodic flush task — prefer running event loop, fall back
        # to a dedicated background thread.
        try:
            loop = asyncio.get_running_loop()
            self._flush_task = loop.create_task(self._periodic_flush())
        except RuntimeError:
            self._start_background_thread()

    def _start_background_thread(self) -> None:
        def run_background_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._background_loop = loop
            loop.create_task(self._periodic_flush())
            loop.run_until_complete(self._process_queue())

        self._background_thread = threading.Thread(
            target=run_background_loop, daemon=True, name="DatadogLogsHandler"
        )
        self._background_thread.start()

    async def _process_queue(self) -> None:
        while not self._closed:
            await asyncio.sleep(0.01)
            while not self._log_queue.empty():
                try:
                    log_entry = self._log_queue.get_nowait()
                    if log_entry is None:
                        return
                    await self._add_to_batch(log_entry)
                except Exception as e:  # pragma: no cover
                    logger.debug("[obs/datadog_logs] queue process error: %s", e)

    # ── Public logging.Handler hooks ────────────────────────────
    def emit(self, record: LogRecord) -> None:
        if self._closed:
            return
        # Filter excluded loggers (avoid observation loop / DDtrace internal noise).
        if record.name.startswith(self.exclude_loggers):
            return
        try:
            log_entry = self._format_log(record)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._add_to_batch(log_entry))
            except RuntimeError:
                if self._background_thread and self._background_thread.is_alive():
                    try:
                        self._log_queue.put_nowait(log_entry)
                    except Exception:  # queue full → drop oldest
                        pass
        except Exception:
            self.handleError(record)

    def _format_log(self, record: LogRecord) -> Any:
        """Build a Datadog HTTPLogItem from a Python LogRecord."""
        message = self.format(record)

        attributes: dict = {
            "logger.name": record.name,
            "logger.thread_name": record.threadName,
            "level": record.levelname,
        }

        # trace_id / span_id correlation — populated by ddtrace's logging
        # patch (we set DD_LOGS_INJECTION=true in configure()). Stored on the
        # record as `dd.trace_id` / `dd.span_id`.
        for attr_src, attr_dst in (
            ("dd.trace_id", "dd.trace_id"),
            ("dd.span_id", "dd.span_id"),
            ("dd.service", "dd.service"),
            ("dd.env", "dd.env"),
            ("dd.version", "dd.version"),
        ):
            val = getattr(record, attr_src, None)
            if val:
                attributes[attr_dst] = str(val)

        # Exception info — render the stack trace as a flat string so DD UI
        # can syntax-highlight it.
        if record.exc_info and record.exc_info[0] is not None:
            attributes["error.kind"] = record.exc_info[0].__name__
            attributes["error.message"] = str(record.exc_info[1])
            if self.formatter and hasattr(self.formatter, "formatException"):
                attributes["error.stack"] = self.formatter.formatException(record.exc_info)
            else:
                import traceback

                attributes["error.stack"] = "".join(
                    traceback.format_exception(*record.exc_info)
                )

        # Build the standard DD tag set — mirrors the APM span tag schema so
        # filters work across APM ↔ Logs.
        tags = [f"env:{self.env}", f"service:{self.service}"]
        if self.version:
            tags.append(f"version:{self.version}")
        if self.agent_id:
            tags.append(f"agent.id:{self.agent_id}")

        return _HTTPLogItem(
            ddsource=self.source,
            ddtags=",".join(tags),
            hostname=self.hostname,
            message=message,
            service=self.service,
            **attributes,
        )

    # ── Internal batching ───────────────────────────────────────
    async def _add_to_batch(self, log_entry: Any) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            self._log_batch.append(log_entry)
            if len(self._log_batch) >= self.batch_size:
                await self._flush_batch()

    async def _flush_batch(self) -> None:
        if not self._log_batch:
            return
        batch = self._log_batch[:]
        self._log_batch.clear()
        self._last_flush_time = time.time()

        for attempt in range(self.max_retries):
            try:
                body = _HTTPLog(batch)
                await asyncio.wait_for(
                    asyncio.to_thread(self.logs_api.submit_log, body=body),
                    timeout=self.timeout,
                )
                return
            except TimeoutError:
                logger.debug(
                    "[obs/datadog_logs] submit timeout (attempt %d/%d)",
                    attempt + 1,
                    self.max_retries,
                )
            except Exception as e:
                logger.debug(
                    "[obs/datadog_logs] submit failed (attempt %d/%d): %s",
                    attempt + 1,
                    self.max_retries,
                    e,
                )
            if attempt < self.max_retries - 1:
                await asyncio.sleep(2**attempt)
        logger.warning(
            "[obs/datadog_logs] dropped %d records after %d retries",
            len(batch),
            self.max_retries,
        )

    async def _periodic_flush(self) -> None:
        while not self._closed:
            await asyncio.sleep(self.flush_interval)
            if self._lock is None:
                self._lock = asyncio.Lock()
            async with self._lock:
                if time.time() - self._last_flush_time >= self.flush_interval:
                    await self._flush_batch()

    # ── Shutdown ────────────────────────────────────────────────
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._background_thread and self._background_thread.is_alive():
            try:
                self._log_queue.put_nowait(None)
            except Exception:
                pass
        if self._flush_task:
            self._flush_task.cancel()
        # Final flush — best effort.
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._final_flush())
        except RuntimeError:
            if self._background_loop:
                try:
                    asyncio.run_coroutine_threadsafe(self._final_flush(), self._background_loop)
                except Exception:  # pragma: no cover
                    pass
        if self._background_thread and self._background_thread.is_alive():
            self._background_thread.join(timeout=2.0)
        try:
            if hasattr(self.api_client, "close"):
                self.api_client.close()
        except Exception:  # pragma: no cover
            pass
        super().close()

    async def _final_flush(self) -> None:
        try:
            if self._lock is None:
                self._lock = asyncio.Lock()
            async with self._lock:
                await self._flush_batch()
        except Exception:  # pragma: no cover
            pass


# ─── Public API ──────────────────────────────────────────────────
def configure(
    *,
    enabled: bool,
    api_key: str,
    site: str,
    service: str,
    env: str,
    version: Optional[str] = None,
    agent_id: Optional[str] = None,
    min_level: int = logging.DEBUG,
    exclude_loggers: Optional[List[str]] = None,
    batch_size: int = 10,
    flush_interval_seconds: float = 5.0,
) -> None:
    """Activate the DD Logs exporter. Idempotent — second call is a no-op."""
    global ENABLED, _handler
    if not enabled or not api_key:
        ENABLED = False
        return
    if _handler is not None:
        # Already configured — skip silently.
        return
    if not _try_import_datadog_api_client():
        ENABLED = False
        return

    # Enable ddtrace's log injection so dd.trace_id / dd.span_id auto-populate
    # on every LogRecord; the handler's _format_log picks them up.
    os.environ.setdefault("DD_LOGS_INJECTION", "true")
    try:
        from ddtrace import patch as _dd_patch  # type: ignore

        _dd_patch(logging=True)
    except Exception as e:  # pragma: no cover — ddtrace may not be installed
        logger.debug("[obs/datadog_logs] ddtrace logging patch unavailable: %s", e)

    try:
        h = _DatadogAsyncHandler(
            api_key=api_key,
            service=service,
            env=env,
            version=version,
            site=site,
            agent_id=agent_id,
            batch_size=batch_size,
            flush_interval_seconds=flush_interval_seconds,
            exclude_loggers=exclude_loggers,
            level=min_level,
        )
        # Use the same format string as backend's basicConfig so console + DD
        # see identical message bodies.
        h.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        logging.getLogger().addHandler(h)
        _handler = h
        ENABLED = True
        logger.info(f"[obs/datadog_logs] enabled (service={service}, env={env}, site={site})")
    except Exception as e:
        ENABLED = False
        logger.warning(f"[obs/datadog_logs] handler attach failed: {e}")


def flush(timeout_ms: int = 5000) -> None:
    """Force-flush any buffered log records. Best-effort; never raises."""
    if not ENABLED or _handler is None:
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_handler._final_flush())  # type: ignore[attr-defined]
    except RuntimeError:
        if _handler._background_loop is not None:  # type: ignore[attr-defined]
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    _handler._final_flush(),  # type: ignore[attr-defined]
                    _handler._background_loop,  # type: ignore[attr-defined]
                )
                fut.result(timeout=timeout_ms / 1000.0)
            except Exception:  # pragma: no cover
                pass


def shutdown() -> None:
    """Detach the handler from root logger + flush. Idempotent."""
    global ENABLED, _handler
    if _handler is None:
        return
    try:
        logging.getLogger().removeHandler(_handler)
        _handler.close()
    except Exception:  # pragma: no cover
        pass
    _handler = None
    ENABLED = False
