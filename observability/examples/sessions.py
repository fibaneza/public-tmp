"""Session store on Aurora Serverless v2 (PostgreSQL).

LiteLLM has no session concept — it is stateless per call. So the session is
*yours*: FastAPI mints the id and owns the history, and LiteLLM receives the id
as call metadata. This module is the "owns the history" half.

------------------------------------------------------------------------------
WHY THE SHAPE OF THIS FILE IS DRIVEN BY AURORA SERVERLESS v2
------------------------------------------------------------------------------
Three properties change the design relative to a key-value store:

  1. CONNECTIONS ARE THE SCARCE RESOURCE, not throughput. Aurora's
     max_connections scales with ACUs, and ECS multiplies: tasks x pool_size.
     Ten tasks with a pool of 20 is 200 connections before you have any load.
     Keep per-task pools small and put RDS Proxy in front — see POOLING below.

  2. THERE IS NO TTL. A key-value store expires rows for you; Postgres does not. Expired
     sessions accumulate silently until the table is mostly dead rows and
     autovacuum is the top consumer of your ACUs. `delete_expired()` below has
     to actually be scheduled — that is the single most common way this design
     rots.

  3. YOU GET TRANSACTIONS AND SQL. That is a real gain: the session update and
     the usage-ledger insert commit together, and per-user cost becomes an
     exact SQL query instead of only a log query. See `llm_usage` below.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import asyncpg

DB_HOST = os.environ.get("DB_HOST", "localhost")          # RDS Proxy endpoint
DB_PORT = int(os.environ.get("DB_PORT", 5432))
DB_NAME = os.environ.get("DB_NAME", "llm")
DB_USER = os.environ.get("DB_USER", "llm_app")
DB_REGION = os.environ.get("AWS_REGION", "eu-west-1")
DB_IAM_AUTH = os.environ.get("DB_IAM_AUTH", "true").lower() == "true"

TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", 60 * 60 * 24 * 7))
MAX_TURNS = int(os.environ.get("SESSION_MAX_TURNS", 40))

# POOLING: deliberately small. This is per ECS *task*, so the cluster-wide
# total is tasks x max_size. With RDS Proxy in front, a small application pool
# is correct — the proxy multiplexes onto far fewer database connections and
# survives failover without the app seeing dropped sockets.
POOL_MIN = int(os.environ.get("DB_POOL_MIN", 1))
POOL_MAX = int(os.environ.get("DB_POOL_MAX", 5))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_sessions (
    session_id      TEXT PRIMARY KEY,
    user_id         TEXT        NOT NULL,
    tenant_id       TEXT        NOT NULL DEFAULT 'default',
    messages        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    turn_count      INTEGER     NOT NULL DEFAULT 0,
    -- Money as integer micro-dollars. NUMERIC would also be correct; what is
    -- NOT correct is float, which accumulates error over a long session.
    cost_micro_usd  BIGINT      NOT NULL DEFAULT 0,
    total_tokens    INTEGER     NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL
);

-- Listing a user's sessions, and deleting them all on an erasure request.
CREATE INDEX IF NOT EXISTS llm_sessions_user_idx
    ON llm_sessions (user_id, updated_at DESC);

-- The cleanup job's index. Without it, delete_expired() sequential-scans the
-- whole table every run, which on Serverless v2 shows up directly as ACUs.
CREATE INDEX IF NOT EXISTS llm_sessions_expires_idx
    ON llm_sessions (expires_at);

-- Optional but recommended on this stack: an exact per-call usage ledger.
-- CloudWatch remains the place for alarms and ad-hoc log forensics; this table
-- is for billing-grade numbers, where "exact" matters more than "queryable
-- alongside the logs".
CREATE TABLE IF NOT EXISTS llm_usage (
    id                BIGSERIAL   PRIMARY KEY,
    request_id        TEXT        NOT NULL,
    session_id        TEXT,
    user_id           TEXT        NOT NULL,
    tenant_id         TEXT        NOT NULL DEFAULT 'default',
    model             TEXT        NOT NULL,
    status            TEXT        NOT NULL,
    input_tokens      INTEGER     NOT NULL DEFAULT 0,
    output_tokens     INTEGER     NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER     NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER    NOT NULL DEFAULT 0,
    cost_micro_usd    BIGINT      NOT NULL DEFAULT 0,
    model_latency_ms  NUMERIC(10,2),
    total_latency_ms  NUMERIC(10,2),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS llm_usage_user_time_idx
    ON llm_usage (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS llm_usage_time_idx
    ON llm_usage (created_at);
"""

# Retention on llm_usage: prefer monthly partitions and DROP the old partition.
# A DELETE of a month of rows is a long transaction that bloats the table and
# spikes ACUs; dropping a partition is instant and reclaims the space.
#
#   CREATE TABLE llm_usage (...) PARTITION BY RANGE (created_at);
#   CREATE TABLE llm_usage_2026_08 PARTITION OF llm_usage
#       FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _iam_token() -> str:
    """Generate a short-lived IAM auth token (valid ~15 minutes).

    Preferred over a password in Secrets Manager: nothing long-lived to rotate,
    and database access is granted by the task role rather than by possession of
    a secret. Requires `rds-db:connect` on the db-user resource, and the
    database user created with `GRANT rds_iam TO llm_app`.
    """
    import boto3

    return boto3.client("rds", region_name=DB_REGION).generate_db_auth_token(
        DBHostname=DB_HOST, Port=DB_PORT, DBUsername=DB_USER, Region=DB_REGION
    )


async def create_pool() -> asyncpg.Pool:
    """Create the connection pool once, at app startup.

    asyncpg accepts a *callable* password, which it invokes per connection —
    that is what makes IAM auth work with a pool: tokens expire after 15
    minutes, and a reconnect after that must mint a fresh one. A token captured
    once at startup produces authentication failures hours later, in a service
    that has been running fine, which is a memorably bad debugging session.
    """
    return await asyncpg.create_pool(
        host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER,
        password=_iam_token if DB_IAM_AUTH else os.environ.get("DB_PASSWORD"),
        ssl="require",                 # Aurora requires TLS for IAM auth
        min_size=POOL_MIN, max_size=POOL_MAX,
        # Recycle connections so a scale event or failover behind RDS Proxy
        # does not leave the pool holding stale sockets.
        max_inactive_connection_lifetime=300.0,
        command_timeout=10.0,
        init=_init_connection,
    )


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Per-connection setup.

    asyncpg returns JSONB as a string unless told otherwise; registering the
    codec here means `messages` arrives as a list, not a str that silently
    works with len() and fails on indexing.
    """
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

@dataclass
class Session:
    session_id: str
    user_id: str
    tenant_id: str = "default"
    messages: list[dict] = field(default_factory=list)
    turn_count: int = 0
    cost_micro_usd: int = 0
    total_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        return self.cost_micro_usd / 1_000_000


def _row_to_session(row: Any) -> Session:
    return Session(
        session_id=row["session_id"], user_id=row["user_id"],
        tenant_id=row["tenant_id"], messages=row["messages"],
        turn_count=row["turn_count"], cost_micro_usd=row["cost_micro_usd"],
        total_tokens=row["total_tokens"],
    )


class SessionStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(self, user_id: str, tenant_id: str = "default") -> Session:
        session_id = f"ses_{uuid.uuid4().hex}"
        row = await self.pool.fetchrow(
            """
            INSERT INTO llm_sessions (session_id, user_id, tenant_id, expires_at)
            VALUES ($1, $2, $3, now() + make_interval(secs => $4))
            RETURNING *
            """,
            session_id, user_id, tenant_id, TTL_SECONDS,
        )
        return _row_to_session(row)

    async def get(self, session_id: str, user_id: str) -> Session | None:
        """Load a session, scoped to its owner and not expired.

        The `user_id` predicate is not belt-and-braces. A session id is a bearer
        token for a conversation, and it travels through logs, metrics and URLs
        where it is easy to observe. Without this clause, anyone who learns one
        reads someone else's history.

        `expires_at > now()` matters because Postgres has no TTL — the row may
        still be present long after it should have been readable.
        """
        row = await self.pool.fetchrow(
            """
            SELECT * FROM llm_sessions
            WHERE session_id = $1 AND user_id = $2 AND expires_at > now()
            """,
            session_id, user_id,
        )
        return _row_to_session(row) if row else None

    async def get_or_create(self, session_id: str | None, user_id: str,
                            tenant_id: str = "default") -> Session:
        if session_id:
            existing = await self.get(session_id, user_id)
            if existing:
                return existing
            # Unknown, expired, or not yours: mint a new one rather than 404.
            # Never resurrect a client-supplied id — that would let a caller
            # choose their own session_id and collide with someone else's.
        return await self.create(user_id, tenant_id)

    async def append_turn(self, session: Session, user_message: str,
                          assistant_message: str, *, cost_micro_usd: int = 0,
                          tokens: int = 0, usage: dict | None = None) -> Session:
        """Append a turn, and optionally write the usage ledger row atomically.

        This is the payoff for being on Postgres: the conversation and the
        billing record commit together. On a key-value store they are two
        independent writes, and a crash between them leaves the ledger and the
        session disagreeing about what the user was charged for.
        """
        messages = session.messages + [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_message},
        ]
        # Bound the history or every turn costs more than the last. A rolling
        # summary of what falls out belongs here in a real system.
        messages = messages[-(MAX_TURNS * 2):]

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE llm_sessions
                       SET messages       = $2,
                           turn_count     = turn_count + 1,
                           total_tokens   = total_tokens + $3,
                           cost_micro_usd = cost_micro_usd + $4,
                           updated_at     = now(),
                           expires_at     = now() + make_interval(secs => $5)
                     WHERE session_id = $1
                 RETURNING *
                    """,
                    session.session_id, messages, tokens, cost_micro_usd, TTL_SECONDS,
                )
                if usage:
                    await conn.execute(
                        """
                        INSERT INTO llm_usage (
                            request_id, session_id, user_id, tenant_id, model, status,
                            input_tokens, output_tokens, cache_read_tokens,
                            cache_write_tokens, cost_micro_usd,
                            model_latency_ms, total_latency_ms)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                        """,
                        usage.get("request_id", ""), session.session_id,
                        session.user_id, session.tenant_id,
                        usage.get("model", ""), usage.get("status", "success"),
                        usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                        usage.get("cache_read_tokens", 0),
                        usage.get("cache_write_tokens", 0),
                        cost_micro_usd,
                        usage.get("model_latency_ms"), usage.get("total_latency_ms"),
                    )
        return _row_to_session(row)

    # -- housekeeping ----------------------------------------------------
    async def delete_expired(self, batch: int = 5_000) -> int:
        """Postgres has no TTL. THIS MUST BE SCHEDULED.

        Deleting in bounded batches rather than one statement keeps the
        transaction short, so it does not hold a snapshot open, block autovacuum
        or spike ACUs. Run it from EventBridge → a small task, or from pg_cron
        inside the cluster.
        """
        deleted = await self.pool.fetchval(
            """
            WITH doomed AS (
                SELECT session_id FROM llm_sessions
                 WHERE expires_at < now()
                 LIMIT $1
                 FOR UPDATE SKIP LOCKED
            )
            DELETE FROM llm_sessions s USING doomed d
             WHERE s.session_id = d.session_id
            RETURNING 1
            """,
            batch,
        )
        return deleted or 0

    async def delete_user(self, user_id: str) -> int:
        """Right-to-erasure: remove a user's conversations.

        Note this deliberately leaves `llm_usage` alone — billing records
        usually have their own retention obligation. Anonymise there instead of
        deleting, and make that a documented decision rather than an accident.
        """
        result = await self.pool.execute(
            "DELETE FROM llm_sessions WHERE user_id = $1", user_id
        )
        return int(result.split()[-1])


# ---------------------------------------------------------------------------
# Per-user cost, as exact SQL
# ---------------------------------------------------------------------------

COST_PER_USER = """
SELECT user_id,
       SUM(cost_micro_usd) / 1000000.0 AS cost_usd,
       SUM(input_tokens)               AS tokens_in,
       SUM(output_tokens)              AS tokens_out,
       SUM(cache_read_tokens)          AS tokens_cached,
       COUNT(*)                        AS calls
  FROM llm_usage
 WHERE created_at >= now() - interval '30 days'
 GROUP BY user_id
 ORDER BY cost_usd DESC
 LIMIT 50
"""

COST_PER_USER_PER_MODEL = """
SELECT user_id, model,
       SUM(cost_micro_usd) / 1000000.0 AS cost_usd,
       COUNT(*)                        AS calls
  FROM llm_usage
 WHERE created_at >= date_trunc('month', now())
 GROUP BY user_id, model
 ORDER BY cost_usd DESC
"""

USERS_OVER_BUDGET = """
SELECT user_id, SUM(cost_micro_usd) / 1000000.0 AS cost_usd
  FROM llm_usage
 WHERE created_at >= date_trunc('day', now())
 GROUP BY user_id
HAVING SUM(cost_micro_usd) > $1
 ORDER BY cost_usd DESC
"""
