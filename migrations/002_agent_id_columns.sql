-- propio-obs-sdk migration 002 — add agent_id to sessions / logs
--
-- Applied to the same Postgres that hosts the existing observability tables
-- (currently propio's monitor DB). Zero-downtime: nullable column + named-column
-- SELECTs in observability_platform mean existing reads are unaffected.
--
-- Apply via:
--   psql $POSTGRES_DB_URL_DEV -f obs_sdk/migrations/002_agent_id_columns.sql
--
-- Reverse:
--   ALTER TABLE sessions DROP COLUMN agent_id;
--   ALTER TABLE logs     DROP COLUMN agent_id;
--   DROP INDEX IF EXISTS idx_logs_agent_session;
--   DROP INDEX IF EXISTS idx_sessions_agent;

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS agent_id TEXT;
ALTER TABLE logs     ADD COLUMN IF NOT EXISTS agent_id TEXT;

CREATE INDEX IF NOT EXISTS idx_logs_agent_session ON logs (agent_id, session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_agent     ON sessions (agent_id, start_time DESC);

-- Optional backfill (commented out — operator runs intentionally when ready):
-- UPDATE sessions SET agent_id = 'propio_agent_pro' WHERE agent_id IS NULL;
-- UPDATE logs     SET agent_id = 'propio_agent_pro' WHERE agent_id IS NULL;
