"""RRR -- Rewrite-Retrieve-Read (Ma et al., 2023, "Query Rewriting for RAG").

Naive RAG is retrieve-then-read: it assumes the user's raw query is a good
search key. It usually is not. Real queries carry typos, chat context
("what about the other one?"), multiple questions at once, and conversational
filler that dilutes the embedding.

RRR inserts a *rewriter* in front:

    query -> [rewrite] -> search query -> [retrieve] -> context -> [read] -> answer

The paper's contribution is that the rewriter is a small trainable model
optimised by reinforcement learning against downstream answer quality -- the
reward is whether the reader got the answer right, not whether the rewrite
looks nice. In production most teams start with the frozen-LLM version below
and only train a rewriter once they have query logs worth learning from.

This file shows three rewriter behaviours that matter in practice:
  * normalise  -- strip filler, fix typos, expand acronyms
  * decompose  -- split a multi-hop question into independent sub-queries
  * step_back  -- ask the more general question first (Zheng et al., 2023)

Run:  python rewrite_retrieve_read.py "so uh whats the refund window and how long till the money lands"
"""

from __future__ import annotations

import sys

from _common import CORPUS, complete, format_context, show
from hybrid_search import HybridRetriever

REWRITE_PROMPT = """Rewrite the user's message into a single, self-contained search
query for a document retrieval system. Remove conversational filler, fix typos,
expand abbreviations, and keep every domain term that carries meaning.
Output only the query.

User message: {question}"""

DECOMPOSE_PROMPT = """Break the question below into the minimum set of independent
sub-questions that must each be answered separately to answer it fully.
If it is already atomic, output it unchanged. One per line, no numbering.

Question: {question}"""

STEP_BACK_PROMPT = """Given the specific question below, write the more general
"step-back" question whose answer provides the background needed to answer it.

Example:
  Specific: Was Aristotle's teacher born before 400 BC?
  Step-back: Who was Aristotle's teacher, and when were they born?

Specific: {question}
Step-back:"""


def rewrite(question: str) -> str:
    return complete(REWRITE_PROMPT.format(question=question)).strip()


def decompose(question: str) -> list[str]:
    raw = complete(DECOMPOSE_PROMPT.format(question=question))
    return [line.strip("-• ").strip() for line in raw.splitlines() if line.strip()]


def step_back(question: str) -> str:
    return complete(STEP_BACK_PROMPT.format(question=question)).strip()


READ_PROMPT = """Answer the question using only the context. Cite the bracketed
source marker after each claim. If the context is insufficient, say so.

Context:
{context}

Question: {question}
"""


def rewrite_retrieve_read(question: str, retriever: HybridRetriever, k: int = 4) -> str:
    # 1. REWRITE -- one normalisation pass plus a decomposition pass. A question
    #    that decomposes into several sub-questions needs several retrievals;
    #    running one retrieval for a compound query retrieves the average of two
    #    topics, which is usually neither.
    normalised = rewrite(question)
    sub_questions = decompose(normalised)
    print(f"\nrewritten : {normalised}")
    print(f"sub-queries: {sub_questions}")

    # 2. RETRIEVE -- union of the per-sub-question hits, deduplicated by id.
    seen, context_chunks = set(), []
    for sub in sub_questions:
        for chunk, _score in retriever.rrf(sub, k=k):
            if chunk.id not in seen:
                seen.add(chunk.id)
                context_chunks.append(chunk)
    show("RRR retrieved context", context_chunks)

    # 3. READ -- the reader sees the union and answers the *original* question,
    #    not the rewrite. The rewrite is a retrieval device, not the user intent.
    return complete(READ_PROMPT.format(
        context=format_context(context_chunks), question=question
    ))


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else (
        "so uh whats the refund window and how long till the money actually lands"
    )
    print("\n" + rewrite_retrieve_read(q, HybridRetriever(CORPUS)))
