"""The full pipeline: every technique in this folder, wired together.

This is the reference architecture the individual example files decompose. Read
them first for the *why*; read this for the ordering and the seams.

    ingest:   parse (Docling) -> structural chunk -> contextualise
              -> embed + BM25 index -> store with doc metadata

    query:    contextualise follow-up (memory)
              -> rewrite / decompose (RRR)
              -> multi-query (RAG-Fusion)
              -> hybrid retrieve per query (dense + BM25)
              -> RRF merge
              -> cross-encoder rerank
              -> parent expansion (small-to-big)
              -> contextual compression
              -> generate with cited claims
              -> verify citations
              -> log for evaluation

Ordering rules worth internalising, because getting them wrong is the usual
cause of a pipeline that is slow *and* inaccurate:

  * Cheap-and-broad before expensive-and-narrow. Hybrid retrieval sees 10k
    chunks; the cross-encoder sees 50; the LLM sees 5.
  * Rerank BEFORE compression. Compression is per-chunk work -- do it only on
    the chunks that survived.
  * Expand to parents AFTER reranking. The reranker wants precise child text;
    the LLM wants the surrounding context.
  * Verify citations AFTER generation. It is the only step that can catch a
    confidently wrong answer, and it needs the answer to exist.

Everything is toggleable so you can ablate one stage at a time against
ragas_eval.py -- which is the only honest way to know whether a stage earns its
latency on *your* corpus.

Run:  python production_rag_pipeline.py "how do I roll back safely?"
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from _common import CORPUS, Chunk, complete, format_context
from citations import answer_with_citations, render, verify
from contextual_compression import compress
from cross_encoder_rerank import rerank
from hybrid_search import HybridRetriever
from rag_fusion import generate_queries, reciprocal_rank_fusion


@dataclass
class PipelineConfig:
    """Every stage is optional. Ablate one at a time and measure."""

    use_query_expansion: bool = True     # RAG-Fusion / multi-query
    use_hybrid: bool = True              # dense + BM25 vs dense only
    use_reranking: bool = True           # cross-encoder second stage
    use_compression: bool = True         # contextual compression
    use_citations: bool = True           # structured claims + verification

    n_query_variants: int = 3
    candidates: int = 20                 # first-stage recall pool
    rerank_to: int = 5                   # what the LLM actually sees
    final_k: int = 4


@dataclass
class Trace:
    """Per-stage record. Log this for every production query: it is your
    evaluation dataset, your latency budget breakdown, and the only way to debug
    "why did it answer that?" after the fact."""

    queries: list[str] = field(default_factory=list)
    candidate_ids: list[str] = field(default_factory=list)
    reranked_ids: list[str] = field(default_factory=list)
    final_ids: list[str] = field(default_factory=list)
    context_chars: int = 0
    verified_claims: int = 0
    total_claims: int = 0


class RAGPipeline:
    def __init__(self, chunks: list[Chunk], config: PipelineConfig | None = None):
        self.config = config or PipelineConfig()
        self.retriever = HybridRetriever(chunks)

    # -- stage 1: query understanding -------------------------------------
    def _expand(self, question: str, trace: Trace) -> list[str]:
        queries = (generate_queries(question, n=self.config.n_query_variants)
                   if self.config.use_query_expansion else [question])
        trace.queries = queries
        return queries

    # -- stage 2: first-stage retrieval (recall) --------------------------
    def _retrieve(self, queries: list[str], trace: Trace) -> list[Chunk]:
        ranked_lists = [
            self.retriever.rrf(q, k=self.config.candidates) if self.config.use_hybrid
            else self.retriever.dense(q, k=self.config.candidates)
            for q in queries
        ]
        fused = reciprocal_rank_fusion(ranked_lists, k=self.config.candidates)
        candidates = [c for c, _ in fused]
        trace.candidate_ids = [c.id for c in candidates]
        return candidates

    # -- stage 3: reranking (precision) -----------------------------------
    def _rerank(self, question: str, candidates: list[Chunk], trace: Trace) -> list[Chunk]:
        if not self.config.use_reranking:
            return candidates[: self.config.rerank_to]
        reranked = [c for c, _ in rerank(question, candidates, top_n=self.config.rerank_to)]
        trace.reranked_ids = [c.id for c in reranked]
        return reranked

    # -- stage 4: context construction ------------------------------------
    def _build_context(self, question: str, chunks: list[Chunk], trace: Trace) -> list[Chunk]:
        final = compress(question, chunks) if self.config.use_compression else chunks
        final = final[: self.config.final_k]
        trace.final_ids = [c.id for c in final]
        trace.context_chars = sum(len(c.text) for c in final)
        return final

    # -- stage 5: generation ----------------------------------------------
    def ask(self, question: str) -> tuple[str, Trace]:
        trace = Trace()
        queries = self._expand(question, trace)
        candidates = self._retrieve(queries, trace)
        reranked = self._rerank(question, candidates, trace)
        context = self._build_context(question, reranked, trace)

        if not self.config.use_citations:
            return complete(
                "Answer using only the context. Say so if it is insufficient.\n\n"
                f"Context:\n{format_context(context)}\n\nQuestion: {question}"
            ), trace

        claims, shortfall = answer_with_citations(question, context)
        claims = verify(claims, context)
        trace.total_claims = len(claims)
        trace.verified_claims = sum(1 for c in claims if c.verified)
        if shortfall and not claims:
            return f"The documents do not answer this. {shortfall}", trace
        return render(claims, context), trace


if __name__ == "__main__":
    question = sys.argv[1] if len(sys.argv) > 1 else "how do I roll back safely?"
    pipeline = RAGPipeline(CORPUS)
    answer, trace = pipeline.ask(question)

    print(f"\n{'=' * 70}\nANSWER\n{'=' * 70}\n{answer}")
    print(f"\n{'=' * 70}\nTRACE\n{'=' * 70}")
    print(f"  queries    : {trace.queries}")
    print(f"  candidates : {trace.candidate_ids}")
    print(f"  reranked   : {trace.reranked_ids}")
    print(f"  final      : {trace.final_ids} ({trace.context_chars} chars)")
    print(f"  citations  : {trace.verified_claims}/{trace.total_claims} verified")
