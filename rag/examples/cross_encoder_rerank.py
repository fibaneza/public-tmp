"""Cross-encoder reranking: the single highest-leverage upgrade to naive RAG.

Retrieval is a recall problem; ranking is a precision problem. A bi-encoder has
to embed the document *before it has seen the query*, so the two never interact
-- that independence is exactly what makes ANN search over millions of vectors
possible, and exactly why the ranking is coarse.

A cross-encoder concatenates query and document into one input and runs full
cross-attention over the pair:

    bi-encoder:    sim( f(query), f(doc) )         -- one vector each, precomputable
    cross-encoder: g( [query; doc] ) -> score      -- one forward pass per pair

That is O(candidates) model calls per query, so it cannot search a corpus. The
standard architecture is therefore two-stage retrieve-and-rerank:

    hybrid retrieval  top-50  (cheap, high recall)
        -> cross-encoder      top-5  (expensive, high precision)
            -> LLM

Typical effect: +10-25 points nDCG@5 over dense-only, and it fixes the failure
that hurts most -- the right chunk sitting at rank 8 when you only pass 5.

Model families:
  * cross-encoder/ms-marco-MiniLM-L-6-v2 -- tiny, ~10ms for 50 pairs on CPU
  * BAAI/bge-reranker-v2-m3              -- multilingual, strong default
  * Cohere Rerank / Voyage rerank        -- hosted API, no GPU to operate
  * ColBERT (late interaction)           -- middle ground: per-token vectors,
                                            precomputable, MaxSim at query time

Run:  python cross_encoder_rerank.py "what stops a retried payment being charged twice?"
"""

from __future__ import annotations

import sys

from _common import CORPUS, Chunk, cross_encoder, show
from hybrid_search import HybridRetriever


def rerank(query: str, candidates: list[Chunk], top_n: int = 3) -> list[tuple[Chunk, float]]:
    """Score every (query, chunk) pair with the cross-encoder and re-sort.

    Scores are raw logits: comparable *within* one query's candidate list, not
    across queries. Use them for ordering; if you need an absolute cutoff,
    calibrate a threshold on your own labelled set rather than guessing.
    """
    pairs = [(query, c.text) for c in candidates]
    scores = cross_encoder().predict(pairs)  # batched internally
    ranked = sorted(zip(candidates, scores), key=lambda cs: -cs[1])
    return [(c, float(s)) for c, s in ranked[:top_n]]


def retrieve_and_rerank(query: str, retriever: HybridRetriever,
                        candidates: int = 20, top_n: int = 3):
    """The two-stage pipeline.

    Tuning rule of thumb: `candidates` should be 4-10x `top_n`. Too few and the
    reranker cannot fix a recall miss (it can only reorder what it is given);
    too many and latency grows linearly for diminishing precision.
    """
    shortlist = [c for c, _ in retriever.rrf(query, k=candidates)]
    return shortlist, rerank(query, shortlist, top_n=top_n)


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else (
        "what stops a retried payment being charged twice?"
    )
    shortlist, reranked = retrieve_and_rerank(q, HybridRetriever(CORPUS), candidates=6, top_n=3)

    show("stage 1 - hybrid retrieval (recall)", shortlist)
    show("stage 2 - cross-encoder rerank (precision)",
         [c for c, _ in reranked], [s for _, s in reranked])

    before = [c.id for c in shortlist[:3]]
    after = [c.id for c, _ in reranked]
    print(f"\ntop-3 before: {before}\ntop-3 after : {after}")
