"""Bedrock Knowledge Base RAG via **RetrieveAndGenerate**.

Two RAG shapes exist for Bedrock Knowledge Bases, and they are *not*
interchangeable — pick deliberately:

1. **Retrieve** (``strands_tools.retrieve``) — returns raw chunks; *your*
   Strands agent model writes the answer. One generation, agent stays in
   control of tone/format, you assemble citations yourself.

2. **RetrieveAndGenerate** (this module) — Bedrock retrieves **and**
   generates a grounded answer *with citations* in a single managed call.
   Less code, built-in citations, consistent RAG quality — but the answer is
   produced by the model **inside Bedrock**, not by your agent.

The catch when you expose #2 as an agent tool: the agent's model then calls
a tool that *already generated an answer*, and may re-generate on top of it —
"double generation". So we do two things:
  * keep the tool call **single-shot** (no KB-side session threading) and let
    AgentCore Memory own conversation continuity, and
  * instruct the agent (in the system prompt) to treat the tool's answer as
    authoritative and pass it through with its citations, not paraphrase it.

If you want pure doc Q&A with no agent orchestration at all, call
``retrieve_and_generate`` directly and skip the agent (see README).

--------------------------------------------------------------------------
modelArn note: RetrieveAndGenerate needs a model **ARN**, not a bare model
id. For on-demand models that's
``arn:aws:bedrock:{region}::foundation-model/{model_id}``. For a
cross-region *inference profile* (ids prefixed ``us.`` / ``eu.`` / ``apac.``)
you must pass the inference-profile ARN/id instead — so we make it
configurable via ``KB_MODEL_ARN`` and only synthesise a foundation-model ARN
as a best-effort fallback.
"""

from __future__ import annotations

from dataclasses import dataclass

import boto3
from strands import tool

from .config import settings


@dataclass
class RagResult:
    """A grounded answer plus its sources and the KB-side session id."""

    answer: str
    citations: list[dict]
    session_id: str | None

    def formatted(self) -> str:
        """Answer with a compact, de-duplicated source list appended."""
        sources: list[str] = []
        for citation in self.citations:
            for ref in citation.get("retrievedReferences", []):
                uri = ref.get("location", {}).get("s3Location", {}).get("uri")
                if uri and uri not in sources:
                    sources.append(uri)
        if not sources:
            return self.answer
        return self.answer + "\n\nSources:\n" + "\n".join(f"- {s}" for s in sources)


def _model_arn() -> str:
    """Resolve the model ARN RetrieveAndGenerate should generate with."""
    if settings.kb_model_arn:
        return settings.kb_model_arn
    # Best-effort fallback: valid for on-demand foundation models. Will NOT
    # work for inference-profile ids (us./eu./apac.) — set KB_MODEL_ARN then.
    return f"arn:aws:bedrock:{settings.region}::foundation-model/{settings.model_id}"


# Module-level client: boto3 clients are thread-safe and cheap to reuse.
_client = boto3.client("bedrock-agent-runtime", region_name=settings.region)


def retrieve_and_generate(query: str, session_id: str | None = None) -> RagResult:
    """Call Bedrock RetrieveAndGenerate for one query.

    Args:
        query: The user's natural-language question.
        session_id: A KB session id returned by a previous call, to continue a
            KB-side conversation. Leave ``None`` for single-shot use (the
            recommended default when AgentCore Memory owns continuity).

    Returns:
        A :class:`RagResult` with the generated answer, citations, and the
        KB session id Bedrock assigned (reuse it only for KB-threaded chats).
    """
    if not settings.kb_enabled:
        raise RuntimeError(
            "Knowledge Base not configured. Set STRANDS_KNOWLEDGE_BASE_ID."
        )

    request: dict = {
        "input": {"text": query},
        "retrieveAndGenerateConfiguration": {
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": settings.knowledge_base_id,
                "modelArn": _model_arn(),
                "retrievalConfiguration": {
                    "vectorSearchConfiguration": {
                        "numberOfResults": settings.kb_num_results,
                    }
                },
            },
        },
    }
    # You may NOT set sessionId on the first call; only reuse one Bedrock gave.
    if session_id:
        request["sessionId"] = session_id

    response = _client.retrieve_and_generate(**request)
    return RagResult(
        answer=response["output"]["text"],
        citations=response.get("citations", []),
        session_id=response.get("sessionId"),
    )


@tool
def kb_search(query: str) -> str:
    """Search the organisation's knowledge base for relevant passages.

    Returns raw passages with their sources and relevance scores. Read them,
    then answer the user's question grounded ONLY in these passages, citing
    the sources. If nothing relevant comes back, say so.

    Args:
        query: A focused natural-language search query.
    """
    if not settings.kb_enabled:
        raise RuntimeError(
            "Knowledge Base not configured. Set STRANDS_KNOWLEDGE_BASE_ID."
        )

    vector_cfg: dict = {"numberOfResults": settings.kb_num_results}
    # HYBRID (vector + keyword) generally improves recall, but is only valid
    # on stores that support it (e.g. OpenSearch Serverless with a text
    # field). Leave unset to let Bedrock choose for other stores.
    if settings.kb_search_type:
        vector_cfg["overrideSearchType"] = settings.kb_search_type

    response = _client.retrieve(
        knowledgeBaseId=settings.knowledge_base_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": vector_cfg},
    )

    results = response.get("retrievalResults", [])
    if not results:
        return "No relevant passages found in the knowledge base."

    blocks = []
    for i, r in enumerate(results, 1):
        text = r.get("content", {}).get("text", "")
        uri = r.get("location", {}).get("s3Location", {}).get("uri", "unknown")
        score = r.get("score")
        score_txt = f" (score {score:.3f})" if isinstance(score, (int, float)) else ""
        blocks.append(f"[{i}] source: {uri}{score_txt}\n{text}")
    return "\n\n".join(blocks)


@tool
def knowledge_base_qa(query: str) -> str:
    """Answer a question using the organisation's knowledge base.

    Use this for any question that should be grounded in internal documents
    (policies, product docs, runbooks). The knowledge base returns an answer
    that is already grounded in and cited from source documents — treat that
    answer as authoritative and preserve its citations.

    Args:
        query: The question to answer from the knowledge base.
    """
    # Single-shot: no KB session threading. AgentCore Memory owns continuity,
    # so we deliberately drop the returned KB session id here.
    return retrieve_and_generate(query).formatted()
