"""RAG-Fusion: one query in, several queries out, one fused ranking back.

A single query is a single sample of what the user meant. RAG-Fusion asks the
LLM for N paraphrases that attack the question from different angles, runs
retrieval for each, then merges the N ranked lists with Reciprocal Rank Fusion.

Why it works: a chunk that only one query variant finds is probably an artefact
of that phrasing. A chunk that *several* independent variants surface is
robustly relevant. RRF turns "appears high in many lists" into a score, so
consensus wins without any score calibration.

Distinguish it from its neighbours:
  * Multi-Query retrieval = same generate-N-queries step, but merges by simple
    deduplicated union rather than by rank fusion. Cheaper, less precise.
  * Hybrid search fuses *different retrievers* over one query. RAG-Fusion fuses
    *different queries* over one retriever. They compose -- run RAG-Fusion where
    each variant uses hybrid retrieval, then fuse everything at once.

Run:  python rag_fusion.py "why did the refund not arrive?"
"""

from __future__ import annotations

import sys
from collections import defaultdict

from _common import CORPUS, Chunk, complete, show
from hybrid_search import HybridRetriever

MULTI_QUERY_PROMPT = """You are helping a search engine.
Generate {n} different versions of the question below, each phrased to surface a
different facet of it: use synonyms, expand acronyms, make implicit assumptions
explicit. Output one query per line, no numbering, no commentary.

Question: {question}"""


def generate_queries(question: str, n: int = 4) -> list[str]:
    raw = complete(MULTI_QUERY_PROMPT.format(n=n, question=question))
    variants = [line.strip("-• ").strip() for line in raw.splitlines() if line.strip()]
    # Always keep the original: the paraphraser sometimes drifts, and the
    # user's own wording is the one phrasing we know is on-intent.
    return [question] + variants[:n]


def reciprocal_rank_fusion(
    ranked_lists: list[list[tuple[Chunk, float]]], k: int = 5, kappa: int = 60
) -> list[tuple[Chunk, float]]:
    fused: dict[str, float] = defaultdict(float)
    by_id: dict[str, Chunk] = {}
    for ranked in ranked_lists:
        for rank, (chunk, _score) in enumerate(ranked, start=1):
            fused[chunk.id] += 1.0 / (kappa + rank)
            by_id[chunk.id] = chunk
    order = sorted(fused.items(), key=lambda kv: -kv[1])[:k]
    return [(by_id[cid], score) for cid, score in order]


def rag_fusion(question: str, retriever: HybridRetriever, k: int = 4, n: int = 4):
    queries = generate_queries(question, n=n)
    print("\n--- generated queries ---")
    for q in queries:
        print(f"  * {q}")

    # Each variant retrieves with hybrid search, so every list is already the
    # product of a dense+sparse fusion. Nesting fusion like this is fine: RRF
    # is associative over rank lists.
    ranked_lists = [retriever.rrf(q, k=6) for q in queries]
    return reciprocal_rank_fusion(ranked_lists, k=k)


if __name__ == "__main__":
    question = sys.argv[1] if len(sys.argv) > 1 else "why did the refund not arrive?"
    hits = rag_fusion(question, HybridRetriever(CORPUS))
    show("RAG-Fusion results", [c for c, _ in hits], [s for _, s in hits])
