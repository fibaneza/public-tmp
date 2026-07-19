"""Session + long-term memory via AgentCore Memory (declarative path).

This module builds an ``AgentCoreMemorySessionManager``. Handing that to a
Strands ``Agent`` gives you, for free:

* **Short-term memory** — the turn-by-turn history of the current session
  is loaded before each response and each new turn is persisted after.
* **Long-term memory** — whatever the Memory resource's strategies have
  distilled (user preferences, semantic facts, session summaries) is
  retrieved and injected into context, scoped by the namespaces in
  ``retrieval_config``.

We prefer this over hand-written lifecycle hooks (see ``memory_hooks.py``)
because the save/load correctness is owned by the SDK, not by us. Reach for
hooks only when you need bespoke retrieval or multi-agent shared memory.
"""

from __future__ import annotations

from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig,
    RetrievalConfig,
)
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)

from .config import settings


def _retrieval_config(actor_id: str) -> dict[str, RetrievalConfig]:
    """Which long-term namespaces to pull into context, and how selectively.

    ``top_k`` caps how many memories are retrieved; ``relevance_score`` is the
    minimum similarity to include one. Tighter (higher) thresholds keep the
    prompt focused; looser (lower) thresholds recall more at the cost of noise.

    The ``{actorId}`` / ``{sessionId}`` placeholders are resolved by the SDK,
    so these keys must match the ``namespaceTemplates`` used when the Memory
    resource was created (see scripts/provision_memory.py).
    """
    return {
        # Preferences: few, high-confidence. Wrong preferences are worse than none.
        "/preferences/{actorId}": RetrievalConfig(top_k=5, relevance_score=0.7),
        # Facts: recall broadly, filter loosely — breadth helps grounding.
        "/facts/{actorId}": RetrievalConfig(top_k=10, relevance_score=0.3),
        # Session summaries: moderate on both axes.
        "/summaries/{actorId}/{sessionId}": RetrievalConfig(top_k=5, relevance_score=0.5),
    }


def build_session_manager(
    actor_id: str, session_id: str
) -> AgentCoreMemorySessionManager | None:
    """Return a configured session manager, or ``None`` if memory is disabled.

    Returning ``None`` (rather than raising) lets the agent degrade to a
    stateless conversation when ``BEDROCK_AGENTCORE_MEMORY_ID`` is unset —
    useful for local development and tests.

    Args:
        actor_id: Stable identifier for the end user (the "who").
        session_id: Identifier for this conversation (the "which"). On
            AgentCore Runtime this is supplied by ``context.session_id``.
    """
    if not settings.memory_enabled:
        return None

    config = AgentCoreMemoryConfig(
        memory_id=settings.memory_id,
        actor_id=actor_id,
        session_id=session_id,
        retrieval_config=_retrieval_config(actor_id),
    )
    return AgentCoreMemorySessionManager(config, region_name=settings.region)
