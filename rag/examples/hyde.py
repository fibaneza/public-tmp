"""HyDE -- Hypothetical Document Embeddings (Gao et al., 2022).

The problem it solves: a question and its answer are different *kinds* of text.
"How long do I have to ask for a refund?" is short, interrogative and vague;
the passage that answers it is declarative and full of domain nouns. Embedding
models are trained mostly on symmetric similarity, so the question vector often
sits closer to *other questions* than to the answer passage.

HyDE closes that gap by never embedding the question at all:

  1. Ask the LLM to *hallucinate* an answer -- a "hypothetical document".
  2. Embed the hypothetical document.
  3. Retrieve with that vector.

The hypothetical answer may be factually wrong; that does not matter. It only
has to be wrong in the right *style* and vocabulary, so its embedding lands in
the neighbourhood where the real answer lives. The retrieved chunks are real,
so the final generation is still grounded.

Cost: one extra LLM call per query (~300-800ms). Skip HyDE for keyword-ish
queries -- inventing a document around "PIT" adds noise, not signal.

Run:  python hyde.py "how long do customers have to ask for a refund?"
"""

from __future__ import annotations

import sys

import numpy as np

from _common import CORPUS, Chunk, complete, embedder, show

HYDE_PROMPT = """Write a short passage (2-3 sentences) that would plausibly answer
the question below, as if excerpted from internal product documentation.
Be specific and confident. Do not hedge, do not mention that you are unsure,
and do not restate the question.

Question: {question}
Passage:"""


def generate_hypothetical(question: str, n: int = 1) -> list[str]:
    """Generate n hypothetical answers.

    n > 1 ("HyDE with multiple generations") averages the embeddings, which
    smooths out the case where a single hallucination drifts off-topic.
    """
    return [complete(HYDE_PROMPT.format(question=question)) for _ in range(n)]


class HydeRetriever:
    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self.matrix = embedder().encode(
            [c.text for c in chunks], normalize_embeddings=True, show_progress_bar=False
        )

    def search(self, question: str, k: int = 3, n_hypotheses: int = 1,
               include_query: bool = True) -> list[tuple[Chunk, float]]:
        docs = generate_hypothetical(question, n=n_hypotheses)
        for i, doc in enumerate(docs, 1):
            print(f"\n--- hypothetical document {i} ---\n{doc}")

        # Averaging the raw question vector back in ("HyDE + query") is a cheap
        # safety net: if the hypothesis drifted, the real question pulls the
        # centroid back toward the literal intent.
        texts = docs + ([question] if include_query else [])
        vectors = embedder().encode(texts, normalize_embeddings=True, show_progress_bar=False)
        centroid = vectors.mean(axis=0)
        centroid /= np.linalg.norm(centroid)

        scores = self.matrix @ centroid
        return [(self.chunks[i], float(scores[i])) for i in np.argsort(-scores)[:k]]


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "how long do customers have to ask for a refund?"
    hits = HydeRetriever(CORPUS).search(q, k=3)
    show("HyDE results", [c for c, _ in hits], [s for _, s in hits])
