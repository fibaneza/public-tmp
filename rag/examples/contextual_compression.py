"""Contextual compression: shrink retrieved context before it reaches the LLM.

Retrieval returns whole chunks. A chunk that is relevant is rarely relevant in
*every sentence* -- typically two sentences answer the question and eight are
neighbouring prose that came along because they shared a chunk boundary. Those
eight sentences cost tokens, push the answer toward the middle of the context
window where attention is weakest ("lost in the middle", Liu et al. 2023), and
give the model more surface to get distracted by.

Contextual compression post-processes the retrieved set, query-aware:

  1. Filter    -- drop whole chunks below a relevance threshold.
  2. Extract   -- keep only the sentences in each surviving chunk that bear on
                  the query (LLM extractor, or embedding similarity per sentence).
  3. Reorder   -- put the strongest chunks at the *edges* of the context.

Two implementations are shown: an embeddings-only filter (fast, free, no LLM)
and an LLM extractor (slower, much sharper). LangChain packages the same idea as
ContextualCompressionRetriever + LLMChainExtractor / EmbeddingsFilter.

Run:  python contextual_compression.py "what happens to in-flight authorisations during rollback?"
"""

from __future__ import annotations

import re
import sys

import numpy as np

from _common import CORPUS, Chunk, complete, embedder, show
from hybrid_search import HybridRetriever


def split_sentences(text: str) -> list[str]:
    """Good-enough sentence splitter. Use spaCy/pysbd if your corpus has
    abbreviations, decimals or citations that break on a naive period."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def embedding_filter(chunks: list[Chunk], query: str, threshold: float = 0.30) -> list[Chunk]:
    """Stage 1: drop chunks whose cosine similarity to the query is below a floor.

    Retrieval always returns k results, even when only two are any good --
    top-k has no notion of "nothing else qualifies". This restores one.
    """
    q = embedder().encode([query], normalize_embeddings=True)[0]
    vectors = embedder().encode([c.text for c in chunks], normalize_embeddings=True,
                                show_progress_bar=False)
    scores = vectors @ q
    return [c for c, s in zip(chunks, scores) if s >= threshold]


def sentence_compress(chunk: Chunk, query: str, keep: int = 2) -> Chunk:
    """Stage 2a: embeddings-only extraction -- keep the top sentences by cosine.

    Cheap and deterministic, but it scores each sentence in isolation, so a
    sentence whose meaning depends on the previous one ("It settles in 3 days")
    can be kept without its antecedent. Preserving original order, as below,
    limits the damage.
    """
    sentences = split_sentences(chunk.text)
    if len(sentences) <= keep:
        return chunk
    q = embedder().encode([query], normalize_embeddings=True)[0]
    vectors = embedder().encode(sentences, normalize_embeddings=True, show_progress_bar=False)
    best = sorted(np.argsort(-(vectors @ q))[:keep])  # sorted() restores reading order
    return Chunk(**{**chunk.__dict__, "text": " ".join(sentences[i] for i in best)})


EXTRACT_PROMPT = """Extract only the parts of the passage that help answer the question.
Copy them verbatim -- do not paraphrase, do not add anything.
If nothing in the passage is relevant, output exactly: NONE

Question: {question}
Passage: {passage}
Extracted:"""


def llm_compress(chunk: Chunk, query: str) -> Chunk | None:
    """Stage 2b: LLM extractor. Sharper than cosine because it understands the
    question, and it can return NONE -- which doubles as a relevance filter.
    Costs one call per chunk, so run it concurrently and only on the shortlist."""
    extracted = complete(EXTRACT_PROMPT.format(question=query, passage=chunk.text)).strip()
    if extracted.upper().startswith("NONE"):
        return None
    return Chunk(**{**chunk.__dict__, "text": extracted})


def reorder_lost_in_the_middle(chunks: list[Chunk]) -> list[Chunk]:
    """Stage 3: LLMs attend most strongly to the start and end of a long context.
    Interleave so the highest-ranked chunks sit at both edges and the weakest
    end up buried in the middle."""
    head, tail = [], []
    for i, chunk in enumerate(chunks):
        (head if i % 2 == 0 else tail).append(chunk)
    return head + tail[::-1]


def compress(query: str, chunks: list[Chunk], use_llm: bool = False) -> list[Chunk]:
    survivors = embedding_filter(chunks, query)
    if use_llm:
        compressed = [c for c in (llm_compress(c, query) for c in survivors) if c]
    else:
        compressed = [sentence_compress(c, query) for c in survivors]
    return reorder_lost_in_the_middle(compressed)


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else (
        "what happens to in-flight authorisations during rollback?"
    )
    retrieved = [c for c, _ in HybridRetriever(CORPUS).rrf(q, k=5)]
    show("before compression", retrieved)

    after = compress(q, retrieved, use_llm=False)
    show("after compression", after)

    before_chars = sum(len(c.text) for c in retrieved)
    after_chars = sum(len(c.text) for c in after)
    print(f"\ncontext: {before_chars} -> {after_chars} chars "
          f"({100 * after_chars / max(before_chars, 1):.0f}% retained)")
