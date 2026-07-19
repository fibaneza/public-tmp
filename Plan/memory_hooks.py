"""Hook-based memory integration (ALTERNATIVE to memory.py).

Prefer ``memory.py`` (the declarative ``AgentCoreMemorySessionManager``) for
almost everything. Use these hooks only when you need control the session
manager doesn't give you, e.g.:

* custom context assembly (re-ranking, summarising before injection),
* multi-agent shared memory with bespoke namespacing,
* writing to / reading from stores other than the defaults.

With hooks, Strands calls you at lifecycle points and *you* own the Memory
calls — which means you also own their correctness. That is the whole
trade-off: flexibility in exchange for maintenance.

Usage::

    from bedrock_agentcore.memory import MemoryClient
    from app.memory_hooks import MemoryHook

    client = MemoryClient(region_name=settings.region)
    agent = Agent(
        model=...,
        hooks=[MemoryHook(client, settings.memory_id, actor_id, session_id)],
    )
"""

from __future__ import annotations

from strands.hooks import (
    AgentInitializedEvent,
    HookProvider,
    HookRegistry,
    MessageAddedEvent,
)


class MemoryHook(HookProvider):
    """Loads context on start and persists each message as it is added."""

    def __init__(self, client, memory_id: str, actor_id: str, session_id: str):
        self._client = client
        self._memory_id = memory_id
        self._actor_id = actor_id
        self._session_id = session_id

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(AgentInitializedEvent, self.on_agent_initialized)
        registry.add_callback(MessageAddedEvent, self.on_message_added)

    # --- LOAD -------------------------------------------------------------
    def on_agent_initialized(self, event: AgentInitializedEvent) -> None:
        """Pull recent turns + relevant long-term memories into the prompt."""
        # Short-term: the last K turns of this session.
        turns = self._client.get_last_k_turns(
            memory_id=self._memory_id,
            actor_id=self._actor_id,
            session_id=self._session_id,
            k=5,
        )

        # Long-term: semantic recall of stored facts for this user.
        last_user_text = self._latest_user_text(event)
        facts = []
        if last_user_text:
            facts = self._client.retrieve_memories(
                memory_id=self._memory_id,
                namespace=f"/facts/{self._actor_id}",
                query=last_user_text,
            )

        context_block = self._render_context(turns, facts)
        if context_block:
            event.agent.system_prompt += f"\n\n{context_block}"

    # --- SAVE -------------------------------------------------------------
    def on_message_added(self, event: MessageAddedEvent) -> None:
        """Persist each new message so future sessions can recall it."""
        self._client.create_event(
            memory_id=self._memory_id,
            actor_id=self._actor_id,
            session_id=self._session_id,
            messages=[event.message],
        )

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def _latest_user_text(event: AgentInitializedEvent) -> str:
        messages = getattr(event.agent, "messages", None) or []
        return str(messages[-1]) if messages else ""

    @staticmethod
    def _render_context(turns, facts) -> str:
        parts = []
        if facts:
            parts.append("Known facts about the user:\n" + str(facts))
        if turns:
            parts.append("Recent conversation:\n" + str(turns))
        return "\n\n".join(parts)
