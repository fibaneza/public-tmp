-- Session METADATA registry (agent-owned). NEVER stores conversation content.
-- Content lives in the managed AgentCore Memory service; this is the
-- queryable index the application uses for "list my conversations" and ops.
--
-- Ownership contract (enforced by grants, not convention):
--   * agent role  → writes/reads agent_meta only
--   * app role    → reads agent_meta only; owns the separate `app` schema

CREATE SCHEMA IF NOT EXISTS agent_meta;

CREATE TABLE IF NOT EXISTS agent_meta.session_metadata (
    session_id        TEXT PRIMARY KEY,               -- AgentCore runtime session id
    actor_id          TEXT        NOT NULL,           -- end-user identifier
    memory_id         TEXT,                           -- AgentCore Memory resource id
    model_id          TEXT,                           -- Bedrock model used
    status            TEXT        NOT NULL DEFAULT 'active',
    turn_count        INTEGER     NOT NULL DEFAULT 0,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    attributes        JSONB       NOT NULL DEFAULT '{}'::jsonb  -- future-proof extras
);

-- Primary app-UX query: a user's sessions, most recent first.
CREATE INDEX IF NOT EXISTS idx_session_meta_actor
    ON agent_meta.session_metadata (actor_id, last_activity_at DESC);

-- Roles (adjust names to your environment):
--   CREATE ROLE agent_writer LOGIN;   -- assumed via Data API secret
--   CREATE ROLE app_reader   LOGIN;
GRANT USAGE ON SCHEMA agent_meta TO agent_writer;
GRANT SELECT, INSERT, UPDATE ON agent_meta.session_metadata TO agent_writer;

GRANT USAGE ON SCHEMA agent_meta TO app_reader;
GRANT SELECT ON agent_meta.session_metadata TO app_reader;

-- Deliberately absent:
--   * DELETE for either role (lifecycle handled by status + retention job)
--   * any app-role write on agent_meta
--   * any agent-role access to the `app` schema
