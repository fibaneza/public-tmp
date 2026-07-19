"""Agent factory — assembles the Strands agent from its parts.

Composition boundary (mirrors the architecture doc):

* ``retrieve``      → reads the Bedrock Knowledge Base (RAG).
* ``gateway_tools`` → call your FastAPI backend, wrapped as MCP tools by
                      AgentCore Gateway. This is how the agent touches
                      business data — never a direct DB connection.
* session manager   → conversation + long-term memory.

Keeping construction in one factory keeps the Runtime entrypoint thin and
makes the agent trivially unit-testable (swap in a fake model or no tools).
"""

from __future__ import annotations

from strands import Agent
from strands.models import BedrockModel

from .config import settings
from .memory import build_session_manager

SYSTEM_PROMPT_RETRIEVE = """\
You are a helpful assistant for our application.

Grounding rules:
- Use the `kb_search` tool to fetch relevant passages from the knowledge
  base, then answer from them. Cite the sources you used.
- For business data or actions (users, orders, workflows), call the provided
  backend tools. Never invent IDs, totals, or record state.
- If retrieval and tools return nothing relevant, say so rather than guessing.
"""

SYSTEM_PROMPT_RAG = """\
You are a helpful assistant for our application.

Grounding rules:
- Use the `knowledge_base_qa` tool for questions about internal documents.
  It returns an answer that is ALREADY grounded in and cited from source
  documents. Treat that answer as authoritative: relay it and preserve its
  citations. Do not contradict it or add unsupported detail.
- For business data or actions (users, orders, workflows), call the provided
  backend tools. Never invent IDs, totals, or record state.
- If the tools return nothing relevant, say so rather than guessing.
"""


def _system_prompt() -> str:
    if settings.kb_enabled and settings.kb_mode == "retrieve_and_generate":
        return SYSTEM_PROMPT_RAG
    return SYSTEM_PROMPT_RETRIEVE


def _load_tools() -> list:
    """Collect the tools the agent may call.

    ``retrieve`` is imported lazily so the agent can still start when
    ``strands_tools`` isn't installed or no KB is configured — the import
    cost and dependency only apply when RAG is actually enabled.
    """
    tools: list = []

    if settings.kb_enabled:
        if settings.kb_mode == "retrieve_and_generate":
            # Managed RAG: Bedrock retrieves AND generates a cited answer.
            from .kb import knowledge_base_qa

            tools.append(knowledge_base_qa)
        else:
            # Retrieve-only via the NATIVE Bedrock Retrieve API: passages come
            # back, the agent's model generates once. Preferred when the agent
            # orchestrates multiple tools per turn (avoids double generation).
            from .kb import kb_search

            tools.append(kb_search)

    # Extend with AgentCore Gateway tools (your FastAPI endpoints exposed as
    # MCP tools). Wiring those up is deployment-specific; see the README.
    # tools.extend(load_gateway_tools())

    return tools


def build_agent(actor_id: str, session_id: str) -> Agent:
    """Construct a ready-to-invoke agent for one user + conversation.

    Args:
        actor_id: Authenticated end-user identifier.
        session_id: Conversation identifier (from the runtime in production).
    """
    return Agent(
        model=BedrockModel(model_id=settings.model_id, region_name=settings.region),
        system_prompt=_system_prompt(),
        session_manager=build_session_manager(actor_id, session_id),
        tools=_load_tools(),
    )
