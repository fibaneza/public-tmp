"""Agentic RAG: the model decides whether, what and how many times to retrieve.

Every pipeline so far is a fixed graph -- retrieve once, maybe rerank, generate.
That is optimal for a single-hop factual question and wrong for everything else:

  * "Hi" triggers a pointless retrieval.
  * "Compare our refund window to our idempotency window" needs two retrievals
    over different topics, then a synthesis step.
  * "What is the retry policy?" retrieves nothing useful, and a fixed pipeline
    answers from noise anyway rather than trying a different query.

Agentic RAG makes retrieval a *tool* rather than a stage. The model plans, calls
the tool with its own query, inspects what came back, and decides whether to
answer, search again with different terms, or say it does not know.

The building blocks, all visible below:

  * tool use          -- retrieval is a callable with a schema, not a prefix step
  * routing           -- one tool per corpus, model picks (or picks none)
  * self-grading      -- CRAG's insight: grade the retrieved docs, and only
                         proceed if they are actually relevant
  * bounded looping   -- max_steps, because an unbounded agent will loop

Named variants in the literature:
  * Self-RAG (Asai et al., 2024) -- the model emits reflection tokens deciding
    retrieve/no-retrieve and critiquing its own output.
  * CRAG / Corrective RAG (Yan et al., 2024) -- a lightweight evaluator grades
    retrieval as correct/ambiguous/incorrect and falls back to web search when
    the corpus fails.
  * ReAct -- the general interleaved reason/act loop this file implements.

Run:  python agentic_rag.py "compare the refund window to how long idempotency keys last"
"""

from __future__ import annotations

import json
import sys

import anthropic

from _common import CORPUS, DEFAULT_MODEL, llm
from hybrid_search import HybridRetriever

RETRIEVER = HybridRetriever(CORPUS)

TOOLS = [
    {
        "name": "search_documents",
        "description": (
            "Search the internal payments documentation (runbook, refund policy, "
            "architecture). Call it once per distinct sub-question. Rephrase and call "
            "again if the results do not contain what you need. Do NOT call it for "
            "greetings, chit-chat, or questions about anything outside this corpus."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A focused search query — keywords and domain terms, "
                                   "not the user's full sentence.",
                },
                "doc_id": {
                    "type": "string",
                    "enum": ["runbook", "policy", "architecture"],
                    "description": "Optional: restrict to one document when you know which.",
                },
            },
            "required": ["query"],
        },
    }
]

SYSTEM = """You are a documentation assistant for a payments platform.

Retrieval policy:
- Decide for yourself whether search is needed. Answer directly without searching
  when the question needs no documents.
- Break a compound question into separate searches, one per sub-question.
- After each result, judge whether it actually answers the sub-question. If not,
  search again with different terms (synonyms, expanded acronyms) — at most twice
  per sub-question.
- Ground every factual claim in retrieved text and cite it as [Title p.N].
- If the documents do not contain the answer, say so. Never fill the gap from
  general knowledge."""


def search_documents(query: str, doc_id: str | None = None, k: int = 4) -> str:
    """The tool body. Returns text the model reads, so it carries the citation
    string inline -- the model cannot cite what it cannot see."""
    hits = RETRIEVER.rrf(query, k=k * 2)
    if doc_id:
        hits = [(c, s) for c, s in hits if c.doc_id == doc_id]
    hits = hits[:k]
    if not hits:
        return "No matching passages found."
    return "\n\n".join(f"{c.citation()}\n{c.text}" for c, _ in hits)


def run_agent(question: str, max_steps: int = 6, verbose: bool = True) -> str:
    """The ReAct loop: call the model, execute any tool it asks for, feed the
    result back, repeat until it stops asking for tools.

    `max_steps` is not optional. A model that keeps failing to find something
    will keep rephrasing forever; the bound converts an infinite spend into a
    graceful "I could not find this".
    """
    client = llm()
    messages: list[dict] = [{"role": "user", "content": question}]

    for step in range(max_steps):
        response = client.messages.create(
            model=DEFAULT_MODEL, max_tokens=2048, system=SYSTEM,
            tools=TOOLS, messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return "".join(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if verbose:
                print(f"  [step {step + 1}] search {json.dumps(block.input)}")
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": search_documents(**block.input),
            })
        messages.append({"role": "user", "content": results})

    return "Step budget exhausted before an answer was reached."


if __name__ == "__main__":
    question = sys.argv[1] if len(sys.argv) > 1 else (
        "compare the refund window to how long idempotency keys last"
    )
    print(f"USER: {question}\n")
    try:
        print(f"\nASSISTANT: {run_agent(question)}")
    except (KeyError, anthropic.AnthropicError) as exc:
        print(f"needs ANTHROPIC_API_KEY: {exc}")
