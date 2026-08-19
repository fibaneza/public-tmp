"""Naive RAG: the three-line baseline every other technique is measured against.

    embed(query) -> top-k cosine neighbours -> stuff into a prompt -> generate

Run:  python naive_rag.py "how do I roll back a payments deploy?"

Read this first. Every "advanced" technique in this folder exists because one
specific step below fails in a specific way -- the docstrings in the other
files name which one.
"""

from __future__ import annotations

import sys

import numpy as np

from _common import CORPUS, Chunk, complete, format_context, show


class NaiveRetriever:
    """Dense-only retrieval over an in-memory matrix.

    Real systems swap the brute-force `matrix @ q` for an ANN index (HNSW/IVF)
    in a vector DB, but the semantics are identical -- and at six chunks the
    exact search is both faster and easier to reason about.
    """

    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        model = _embedder()
        # normalize_embeddings=True makes the dot product *be* cosine similarity.
        self.matrix = model.encode(
            [c.text for c in chunks],
            normalize_embeddings=True,
            show_progress_bar=False,
        )

    def search(self, query: str, k: int = 3) -> list[tuple[Chunk, float]]:
        q = _embedder().encode([query], normalize_embeddings=True)[0]
        scores = self.matrix @ q
        top = np.argsort(-scores)[:k]
        return [(self.chunks[i], float(scores[i])) for i in top]


def _embedder():
    from _common import embedder

    return embedder()


PROMPT = """Answer the question using only the context below.
If the context does not contain the answer, say so plainly -- do not guess.

Context:
{context}

Question: {question}
"""


def answer(question: str, retriever: NaiveRetriever, k: int = 3) -> str:
    hits = retriever.search(question, k=k)
    show("naive top-k", [c for c, _ in hits], [s for _, s in hits])
    return complete(PROMPT.format(context=format_context([c for c, _ in hits]),
                                  question=question))


if __name__ == "__main__":
    question = sys.argv[1] if len(sys.argv) > 1 else "How do I roll back a payments deploy?"
    print(answer(question, NaiveRetriever(CORPUS)))
