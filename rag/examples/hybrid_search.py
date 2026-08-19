"""Hybrid search: dense (semantic) + BM25 (lexical), merged with RRF.

Why bother? The two retrievers fail on opposite inputs:

  * Dense search embeds meaning, so it finds "undo a release" ~ "roll back a
    deployment" -- but it blurs rare tokens. Ask for "PIT" and the embedding of
    a three-letter acronym is near-noise.
  * BM25 scores exact term overlap with IDF weighting, so "PIT" and "14 days"
    land instantly -- but it returns nothing for a paraphrase that shares no
    vocabulary with the document.

Fusing them covers both. This file shows the two standard fusion methods:
Reciprocal Rank Fusion (rank-based, no tuning) and weighted score fusion
(score-based, needs normalisation).

Run:  python hybrid_search.py "what is a PIT?"
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict

import numpy as np

from _common import CORPUS, Chunk, embedder, show


def tokenize(text: str) -> list[str]:
    """BM25 needs tokens, not a string. Lowercase + strip punctuation is enough
    here; production systems add stemming and a stopword list."""
    return re.findall(r"[a-z0-9]+", text.lower())


class HybridRetriever:
    def __init__(self, chunks: list[Chunk]):
        from rank_bm25 import BM25Okapi

        self.chunks = chunks
        self.bm25 = BM25Okapi([tokenize(c.text) for c in chunks])
        self.matrix = embedder().encode(
            [c.text for c in chunks], normalize_embeddings=True, show_progress_bar=False
        )

    # -- the two individual retrievers ------------------------------------
    def dense(self, query: str, k: int) -> list[tuple[Chunk, float]]:
        q = embedder().encode([query], normalize_embeddings=True)[0]
        scores = self.matrix @ q
        return [(self.chunks[i], float(scores[i])) for i in np.argsort(-scores)[:k]]

    def sparse(self, query: str, k: int) -> list[tuple[Chunk, float]]:
        scores = self.bm25.get_scores(tokenize(query))
        return [(self.chunks[i], float(scores[i])) for i in np.argsort(-scores)[:k]]

    # -- fusion strategy 1: Reciprocal Rank Fusion ------------------------
    def rrf(self, query: str, k: int = 5, candidates: int = 10, kappa: int = 60):
        """RRF ignores raw scores and uses only *rank*: score = sum 1/(kappa+rank).

        That is its whole appeal -- BM25 scores are unbounded and corpus
        dependent while cosine sits in [-1, 1], so they cannot be added
        directly. Ranks are always comparable. kappa=60 is the value from the
        original Cormack et al. paper and is what almost everyone ships; it
        damps the influence of the very top rank so one confident retriever
        cannot monopolise the fused list.
        """
        fused: dict[str, float] = defaultdict(float)
        by_id = {c.id: c for c in self.chunks}
        for ranked in (self.dense(query, candidates), self.sparse(query, candidates)):
            for rank, (chunk, _score) in enumerate(ranked, start=1):
                fused[chunk.id] += 1.0 / (kappa + rank)
        order = sorted(fused.items(), key=lambda kv: -kv[1])[:k]
        return [(by_id[cid], score) for cid, score in order]

    # -- fusion strategy 2: weighted score fusion -------------------------
    def weighted(self, query: str, k: int = 5, candidates: int = 10, alpha: float = 0.5):
        """alpha * dense + (1 - alpha) * sparse, after min-max normalising each
        side into [0, 1]. More expressive than RRF (you can bias toward lexical
        for a code/ID-heavy corpus) but alpha must be tuned per corpus, and the
        normalisation is only stable if you always normalise over the same
        candidate pool."""
        dense = dict((c.id, s) for c, s in self.dense(query, candidates))
        sparse = dict((c.id, s) for c, s in self.sparse(query, candidates))
        by_id = {c.id: c for c in self.chunks}

        def norm(d: dict[str, float]) -> dict[str, float]:
            if not d:
                return {}
            lo, hi = min(d.values()), max(d.values())
            span = hi - lo or 1.0
            return {key: (v - lo) / span for key, v in d.items()}

        dense, sparse = norm(dense), norm(sparse)
        combined = {
            cid: alpha * dense.get(cid, 0.0) + (1 - alpha) * sparse.get(cid, 0.0)
            for cid in set(dense) | set(sparse)
        }
        order = sorted(combined.items(), key=lambda kv: -kv[1])[:k]
        return [(by_id[cid], score) for cid, score in order]


if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else "what is a PIT?"
    r = HybridRetriever(CORPUS)

    for label, hits in [
        ("dense only", r.dense(query, 3)),
        ("bm25 only", r.sparse(query, 3)),
        ("hybrid (RRF)", r.rrf(query, 3)),
        ("hybrid (weighted a=0.5)", r.weighted(query, 3)),
    ]:
        show(label, [c for c, _ in hits], [s for _, s in hits])
