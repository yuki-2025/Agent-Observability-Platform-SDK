"""Postgres event-mirror + session lifecycle exporter.

SDK-owned replacement for propio_one's monitor_service.py. Same DB, same
tables (`sessions`, `logs`), same `pg_notify('monitor_events', log_id)`
listener contract — but driven from the SDK so every agent gets it for free
and rows are tagged with `agent_id`.

Verbs exposed via api.py:
- `start_session(session_id, *, config, env, agent_id)` — INSERT sessions row, kick heartbeat task
- `end_session(session_id)` — UPDATE sessions, cancel heartbeat
- `broadcast_event(event, *, session_id, agent_id)` — fire-and-forget INSERT logs + pg_notify

Design choices (carried over from monitor_service.py — see lines 54-203 there):
- Fire-and-forget INSERTs via `asyncio.create_task` so the audio pipeline never
  blocks on a PG round-trip
- Strong refs on pending tasks to avoid "Task was destroyed" warnings
- One heartbeat task per live session, cancelled on end_session
- Schema relies on `agent_id` column being present (migration 002). If the
  column is missing the INSERT will raise, but it's fire-and-forget so the
  agent process isn't impacted — error is logged.

Failure isolation:
- Pool init failure → `ENABLED=False`, all verbs become no-ops, agent boots fine
- INSERT failure (transient PG) → logged at ERROR, dropped (matches monitor_service)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)


# Heartbeat: each live session updates `last_heartbeat_at` every N seconds.
# Paired with the observability_platform reaper's 30s timeout — 6× headroom
# for transient DB hiccups. Matches monitor_service.HEARTBEAT_INTERVAL_S.
HEARTBEAT_INTERVAL_S = 5.0


# ─── Module-level lazy import slot for asyncpg ───────────────────
_asyncpg: Optional[Any] = None


def _try_import_asyncpg() -> bool:
    global _asyncpg
    try:
        import asyncpg as _ap

        _asyncpg = _ap
        return True
    except Exception as e:  # pragma: no cover
        logger.warning(f"[obs/postgres_db] asyncpg import failed: {e}")
        return False


class PostgresDBExporter:
    """Owns the asyncpg pool for SDK PG writes. Singleton-like: one per SDK
    init_agent() invocation (init_agent itself is idempotent).
    """

    name = "postgres_db"

    def __init__(self) -> None:
        self.enabled: bool = False
        self.url: str = ""
        self._pool: Optional[Any] = None
        # Strong refs to pending fire-and-forget INSERTs — without these, the
        # asyncio loop is free to GC the task before the SQL completes.
        self._pending_writes: Set[asyncio.Task] = set()
        # One heartbeat task per live session.
        self._heartbeats: Dict[str, asyncio.Task] = {}
        # Initialization state — pool is opened lazily on first write because
        # init_agent() runs in lifespan startup (sync context); asyncpg pool
        # creation needs an event loop.
        self._pool_lock: Optional[asyncio.Lock] = None

    # ─── Lifecycle ──────────────────────────────────────────────
    def setup(self, cfg: Any) -> None:
        """Called by init_agent. Records config but does NOT open the pool
        yet — async pool creation is deferred to the first verb call so it
        runs inside the FastAPI event loop, not in init_agent's sync caller.
        """
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
        if not _try_import_asyncpg():
            return
        self.enabled = True
        logger.info(
            f"[obs/postgres_db] configured (env-var={url_env}); pool opens on first write"
        )

    async def _ensure_pool(self) -> Optional[Any]:
        """Open the pool on first use. Idempotent. Pool size kept small (1–3)
        to coexist with the agent's own asyncpg pool on the same DB."""
        if self._pool is not None:
            return self._pool
        if not self.enabled or _asyncpg is None:
            return None
        if self._pool_lock is None:
            self._pool_lock = asyncio.Lock()
        async with self._pool_lock:
            if self._pool is not None:  # double-check after lock
                return self._pool
            try:
                ssl = "require" if self.url.startswith(("postgresql", "postgres")) and (
                    "sslmode=require" in self.url or "render.com" in self.url
                ) else None
                self._pool = await _asyncpg.create_pool(
                    self.url,
                    ssl=ssl,
                    min_size=1,
                    max_size=3,
                )
                logger.info("[obs/postgres_db] asyncpg pool opened (min=1, max=3)")
            except Exception as e:
                logger.error(f"[obs/postgres_db] pool init failed: {e}")
                self.enabled = False
                self._pool = None
        return self._pool

    def _fire(self, coro) -> None:
        """Fire-and-forget task with a strong ref to prevent GC."""
        task = asyncio.create_task(coro)
        self._pending_writes.add(task)
        task.add_done_callback(self._pending_writes.discard)

    # ─── Verbs ──────────────────────────────────────────────────
    async def start_session(
        self,
        session_id: str,
        *,
        config: Optional[Dict[str, Any]] = None,
        env: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        """Register a new session row + kick heartbeat.

        Phase 1 double-write note: propio_one's legacy monitor_service.start_session
        runs FIRST (no ON CONFLICT, no agent_id). When SDK's INSERT then hits
        the same session_id, ON CONFLICT DO UPDATE backfills `agent_id` onto
        the row so dashboards can filter by agent. After Phase 2 (monitor_service
        deleted), the UPDATE branch becomes dead code — harmless.
        """
        if not self.enabled:
            return
        pool = await self._ensure_pool()
        if pool is None:
            return
        now = datetime.now(timezone.utc)
        config_str = json.dumps(config or {})
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO sessions "
                    "(id, start_time, is_active, config, env, agent_id, last_heartbeat_at) "
                    "VALUES ($1, $2, TRUE, $3::jsonb, $4, $5, $2) "
                    "ON CONFLICT (id) DO UPDATE "
                    "SET agent_id = EXCLUDED.agent_id "
                    "WHERE sessions.agent_id IS NULL",
                    session_id, now, config_str, env or "dev", agent_id,
                )
            logger.info(
                f"[obs/postgres_db] started session {session_id} "
                f"(agent_id={agent_id}, env={env or 'dev'})"
            )
            # Kick heartbeat task; one per session.
            if session_id not in self._heartbeats:
                self._heartbeats[session_id] = asyncio.create_task(
                    self._heartbeat_loop(session_id),
                    name=f"obs-heartbeat-{session_id}",
                )
        except Exception as e:
            logger.error(f"[obs/postgres_db] start_session failed for {session_id}: {e}")

    async def end_session(self, session_id: str) -> None:
        """Cancel heartbeat + mark session ended."""
        if not self.enabled:
            return
        task = self._heartbeats.pop(session_id, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        pool = await self._ensure_pool()
        if pool is None:
            return
        now = datetime.now(timezone.utc)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE sessions SET end_time = $1, is_active = FALSE WHERE id = $2",
                    now, session_id,
                )
            logger.info(f"[obs/postgres_db] ended session {session_id}")
        except Exception as e:
            logger.error(f"[obs/postgres_db] end_session failed for {session_id}: {e}")

    async def broadcast_event(
        self,
        event: Dict[str, Any],
        *,
        session_id: str,
        agent_id: Optional[str] = None,
    ) -> None:
        """Fire-and-forget INSERT into logs + pg_notify('monitor_events', id).

        Returns immediately — the actual SQL runs in a background task.
        Mirrors monitor_service.broadcast's behavior so observability_platform
        sees these rows identically (apart from the new agent_id column).
        """
        if not self.enabled:
            return
        ts = datetime.now(timezone.utc)
        enriched = {
            **event,
            "_monitor": {
                "timestamp": ts.isoformat(),
                "session_id": session_id,
                "agent_id": agent_id,
            },
        }
        payload = json.dumps(enriched, default=str)
        event_type = event.get("type", "unknown")
        self._fire(self._persist_event(session_id, ts, event_type, payload, agent_id))

    async def _persist_event(
        self,
        session_id: str,
        ts: datetime,
        event_type: str,
        payload: str,
        agent_id: Optional[str],
    ) -> None:
        pool = await self._ensure_pool()
        if pool is None:
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "WITH ins AS ("
                    "  INSERT INTO logs (session_id, timestamp, event_type, payload, agent_id) "
                    "  VALUES ($1, $2, $3, $4::jsonb, $5) RETURNING id"
                    ") "
                    "SELECT pg_notify('monitor_events', id::text) FROM ins",
                    session_id, ts, event_type, payload, agent_id,
                )
        except Exception as e:
            logger.error(f"[obs/postgres_db] persist_event failed: {e}")

    async def _heartbeat_loop(self, session_id: str) -> None:
        """Bump `last_heartbeat_at` every HEARTBEAT_INTERVAL_S until cancelled."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                pool = await self._ensure_pool()
                if pool is None:
                    continue
                try:
                    async with pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE sessions SET last_heartbeat_at = now() WHERE id = $1",
                            session_id,
                        )
                except Exception as e:
                    # Don't kill the loop on transient DB hiccups.
                    logger.warning(
                        f"[obs/postgres_db] heartbeat update failed for {session_id}: {e}"
                    )
        except asyncio.CancelledError:
            raise

    # ─── Shutdown ───────────────────────────────────────────────
    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Cancel heartbeats, drain pending writes, close pool.

        Called from api.py's `flush()` (which atexit invokes). Sync entry so
        long-running servers can shutdown cleanly even from a sync context.
        """
        # Cancel heartbeats first (cheap, sync-friendly).
        for sid, task in list(self._heartbeats.items()):
            task.cancel()
        self._heartbeats.clear()

        if self._pool is None:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._async_shutdown(timeout_ms))
        except RuntimeError:
            # No running loop — try to run shutdown in a new one if possible.
            # Best-effort; atexit may race with interpreter teardown.
            try:
                asyncio.run(self._async_shutdown(timeout_ms))
            except Exception:  # pragma: no cover
                pass

    async def _async_shutdown(self, timeout_ms: int) -> None:
        try:
            # Wait briefly for pending writes — bounded by timeout.
            if self._pending_writes:
                await asyncio.wait(
                    self._pending_writes, timeout=timeout_ms / 1000.0
                )
        except Exception:  # pragma: no cover
            pass
        try:
            if self._pool is not None:
                await self._pool.close()
                logger.info("[obs/postgres_db] pool closed")
        except Exception:  # pragma: no cover
            pass
        self._pool = None
        self.enabled = False
