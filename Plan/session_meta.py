"""Session **metadata** registry in Aurora PostgreSQL, via the RDS Data API.

What this is — and is not
-------------------------
Conversation *content* (turns, extracted memories) lives exclusively in the
managed AgentCore Memory service. What the application also needs is a
queryable index for UX and ops: "list my conversations", last activity,
turn counts, admin dashboards. That index is **metadata only** and lives in
the ``agent_meta`` schema of Aurora. No prompts, no responses — ever.

Why the RDS Data API
--------------------
The write path is one small upsert per chat turn. The Data API gives us
HTTPS + IAM auth (credentials via Secrets Manager) with **no connection
pools and no VPC wiring on the Runtime** — the right trade for a tiny,
low-frequency write. If the agent ever needs heavy or low-latency DB access,
switch to AgentCore Runtime VPC mode + psycopg/RDS Proxy; that's a bigger
hammer we don't need yet.

Why fire-and-forget
-------------------
This table is an index, not the record of truth. A metadata write failure is
logged and swallowed — it must never fail a chat turn. Losing one row's
freshness is acceptable; failing a user's message is not.

Prerequisites
-------------
* Aurora PostgreSQL cluster with the **Data API enabled**.
* ``db/schema.sql`` applied.
* Env vars: ``AURORA_CLUSTER_ARN``, ``AURORA_SECRET_ARN``,
  ``AURORA_DATABASE`` (see config.py). Unset → metadata writes are skipped.
* Runtime execution role: ``rds-data:ExecuteStatement`` on the cluster and
  ``secretsmanager:GetSecretValue`` on the secret.
"""

from __future__ import annotations

import logging

import boto3

from .config import settings

logger = logging.getLogger(__name__)

_UPSERT_SQL = """
INSERT INTO agent_meta.session_metadata
    (session_id, actor_id, memory_id, model_id, turn_count,
     started_at, last_activity_at)
VALUES
    (:session_id, :actor_id, :memory_id, :model_id, 1, now(), now())
ON CONFLICT (session_id) DO UPDATE SET
    turn_count       = agent_meta.session_metadata.turn_count + 1,
    last_activity_at = now(),
    status           = 'active'
"""

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("rds-data", region_name=settings.region)
    return _client


def _param(name: str, value: str | None) -> dict:
    if value is None:
        return {"name": name, "value": {"isNull": True}}
    return {"name": name, "value": {"stringValue": value}}


def record_turn(session_id: str, actor_id: str) -> None:
    """Upsert this session's metadata row for the current turn.

    Best-effort by design: any failure is logged at WARNING and swallowed so
    the chat path never depends on Aurora availability. Call it *after* the
    agent has produced its reply.
    """
    if not settings.session_meta_enabled:
        return

    try:
        _get_client().execute_statement(
            resourceArn=settings.aurora_cluster_arn,
            secretArn=settings.aurora_secret_arn,
            database=settings.aurora_database,
            sql=_UPSERT_SQL,
            parameters=[
                _param("session_id", session_id),
                _param("actor_id", actor_id),
                _param("memory_id", settings.memory_id),
                _param("model_id", settings.model_id),
            ],
        )
    except Exception:  # noqa: BLE001 — deliberate: metadata must not break chat
        logger.warning(
            "session metadata upsert failed (session_id=%s); continuing",
            session_id,
            exc_info=True,
        )
