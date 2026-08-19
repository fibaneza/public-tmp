"""Session store: created by FastAPI, shared with LiteLLM.

LiteLLM has no session concept of its own — it is stateless per call. So the
session is *yours*: FastAPI mints the id, owns the history, and passes the id
down as call metadata. LiteLLM never manages it; it only labels telemetry with
it, which is exactly the division of responsibility you want.

    POST /chat            (no session_id)  -> create session, return it
    POST /chat            (session_id)     -> load history, append, save
    every litellm call    metadata={"session_id": ...}

DynamoDB is used here because it fits the access pattern (single-key get/put,
TTL eviction, no connection pool to manage from ECS) and because a session store
that outlives a task restart is a hard requirement on Fargate — tasks are
replaced routinely and in-process dicts vanish with them.

Redis/ElastiCache is the other reasonable choice; swap the two methods.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

TABLE = os.environ.get("SESSION_TABLE", "llm-sessions")
TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", 60 * 60 * 24 * 7))
MAX_TURNS = int(os.environ.get("SESSION_MAX_TURNS", 40))


@dataclass
class Session:
    session_id: str
    user_id: str
    tenant_id: str = "default"
    messages: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    turn_count: int = 0
    # Running totals let you enforce a per-session budget without a query.
    cost_usd: float = 0.0
    total_tokens: int = 0


class SessionStore:
    def __init__(self, table_name: str = TABLE):
        self.table = boto3.resource("dynamodb").Table(table_name)

    # -- lifecycle -------------------------------------------------------
    def create(self, user_id: str, tenant_id: str = "default") -> Session:
        session = Session(session_id=f"ses_{uuid.uuid4().hex}", user_id=user_id,
                          tenant_id=tenant_id)
        self._put(session)
        return session

    def get(self, session_id: str, user_id: str) -> Session | None:
        """Load a session, scoped to its owner.

        The `user_id` check is not belt-and-braces — a session id is a bearer
        token for a conversation. Without this check, anyone who learns or
        guesses an id reads someone else's history, and the id travels through
        logs and metrics where it is easy to observe.
        """
        item = self.table.get_item(Key={"session_id": session_id}).get("Item")
        if not item or item.get("user_id") != user_id:
            return None
        return Session(
            session_id=item["session_id"],
            user_id=item["user_id"],
            tenant_id=item.get("tenant_id", "default"),
            messages=item.get("messages", []),
            created_at=float(item.get("created_at", time.time())),
            turn_count=int(item.get("turn_count", 0)),
            cost_usd=float(item.get("cost_usd", 0)),
            total_tokens=int(item.get("total_tokens", 0)),
        )

    def get_or_create(self, session_id: str | None, user_id: str,
                      tenant_id: str = "default") -> Session:
        if session_id:
            existing = self.get(session_id, user_id)
            if existing:
                return existing
            # Unknown or not-yours: mint a new one rather than 404. Never
            # resurrect a client-supplied id — that would let a caller choose
            # their own session_id and collide with someone else's.
        return self.create(user_id, tenant_id)

    # -- turns -----------------------------------------------------------
    def append_turn(self, session: Session, user_message: str, assistant_message: str,
                    cost_usd: float = 0.0, tokens: int = 0) -> Session:
        session.messages.append({"role": "user", "content": user_message})
        session.messages.append({"role": "assistant", "content": assistant_message})
        # Bound the history or every turn gets more expensive than the last.
        # A rolling summary belongs here in a real system.
        session.messages = session.messages[-(MAX_TURNS * 2):]
        session.turn_count += 1
        session.cost_usd += cost_usd
        session.total_tokens += tokens
        self._put(session)
        return session

    def _put(self, session: Session) -> None:
        self.table.put_item(Item={
            "session_id": session.session_id,
            "user_id": session.user_id,
            "tenant_id": session.tenant_id,
            "messages": session.messages,
            "created_at": int(session.created_at),
            "turn_count": session.turn_count,
            # DynamoDB stores floats as Decimal; keep money in integer
            # micro-dollars to avoid float/Decimal conversion errors on write.
            "cost_micro_usd": int(session.cost_usd * 1_000_000),
            "total_tokens": session.total_tokens,
            "expires_at": int(time.time()) + TTL_SECONDS,   # DynamoDB TTL attribute
        })


# ---------------------------------------------------------------------------
# Table definition, for reference
# ---------------------------------------------------------------------------
CREATE_TABLE = {
    "TableName": TABLE,
    "KeySchema": [{"AttributeName": "session_id", "KeyType": "HASH"}],
    "AttributeDefinitions": [
        {"AttributeName": "session_id", "AttributeType": "S"},
        {"AttributeName": "user_id", "AttributeType": "S"},
    ],
    # Lets you list a user's sessions, and delete them all on an erasure request.
    "GlobalSecondaryIndexes": [{
        "IndexName": "user_id-index",
        "KeySchema": [{"AttributeName": "user_id", "KeyType": "HASH"}],
        "Projection": {"ProjectionType": "KEYS_ONLY"},
    }],
    "BillingMode": "PAY_PER_REQUEST",
    # Enable TTL on `expires_at` separately:
    #   aws dynamodb update-time-to-live --table-name llm-sessions \
    #     --time-to-live-specification "Enabled=true,AttributeName=expires_at"
}
