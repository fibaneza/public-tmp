"""Embeddings for retrieval: the choices that actually move the numbers.

An embedding maps text to a vector such that semantically similar text lands
nearby. For RAG the interesting part is not the definition but the five
decisions you make around it, each demonstrated below:

  1. *Symmetric vs asymmetric.* Retrieval is asymmetric: a short question must
     match a long passage. Models trained for it (E5, BGE, GTE) expect a
     PREFIX -- "query: " vs "passage: ". Omitting the prefix is the single most
     common silent bug in RAG, and it costs several points of recall with no
     error message.
  2. *Normalisation.* Normalise to unit length and the dot product *is* cosine
     similarity, so the fastest index operation is also the correct metric.
  3. *Metric choice.* Cosine for normalised text embeddings; inner product only
     if your model was trained for it; L2 almost never for text.
  4. *Dimensionality.* Matryoshka models (nomic-embed, text-embedding-3, some
     BGE) are trained so a truncated prefix of the vector is still a valid
     embedding -- 3072 dims -> 512 dims is a 6x memory cut for a small recall
     loss, and it composes with a rerank stage that recovers most of it.
  5. *Quantisation.* float32 -> int8 -> binary. Binary + Hamming distance is
     ~32x smaller and very fast; use it as a first-stage funnel and rescore the
     survivors with the full-precision vectors.

Run:  python embeddings_lab.py
"""

from __future__ import annotations

import numpy as np

from _common import embedder

QUERY = "how long do I have to ask for my money back?"
PASSAGES = [
    "Customers may request a refund within 14 days of purchase.",
    "Approved refunds are returned to the original payment method.",
    "The ledger service is append-only and stores immutable entries.",
]


# ---------------------------------------------------------------------------
# 1. Prefixes
# ---------------------------------------------------------------------------

def with_prefixes(query: str, passages: list[str]) -> np.ndarray:
    """BGE/E5-family models are trained with asymmetric instruction prefixes.
    Check your model card -- BGE v1.5 wants the instruction on the query only,
    E5 wants "query: "/"passage: " on both, and several newer models want
    neither. Getting it wrong degrades silently."""
    model = embedder()
    q = model.encode(
        ["Represent this sentence for searching relevant passages: " + query],
        normalize_embeddings=True,
    )[0]
    p = model.encode(passages, normalize_embeddings=True)
    return p @ q


def without_prefixes(query: str, passages: list[str]) -> np.ndarray:
    model = embedder()
    q = model.encode([query], normalize_embeddings=True)[0]
    p = model.encode(passages, normalize_embeddings=True)
    return p @ q


# ---------------------------------------------------------------------------
# 2-3. Normalisation and metrics
# ---------------------------------------------------------------------------

def metrics_demo(a: np.ndarray, b: np.ndarray) -> dict[str, float]:
    """On unit vectors these three are monotonically equivalent -- the ranking is
    identical, only the numbers differ (l2^2 = 2 - 2*cos). On *un*-normalised
    vectors they disagree, and inner product starts rewarding long documents
    simply for having a bigger norm."""
    cosine = float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))
    return {
        "cosine": cosine,
        "dot": float(a @ b),
        "l2": float(np.linalg.norm(a - b)),
    }


# ---------------------------------------------------------------------------
# 4. Matryoshka truncation
# ---------------------------------------------------------------------------

def truncate(vectors: np.ndarray, dims: int) -> np.ndarray:
    """Keep the first `dims` components and re-normalise.

    This is only valid for models trained with Matryoshka Representation
    Learning, which front-loads information into the early dimensions. Truncate
    a conventional embedding and you get noise.
    """
    cut = vectors[..., :dims]
    return cut / np.linalg.norm(cut, axis=-1, keepdims=True)


# ---------------------------------------------------------------------------
# 5. Quantisation
# ---------------------------------------------------------------------------

def binary_quantize(vectors: np.ndarray) -> np.ndarray:
    """1 bit per dimension: sign of each component. 32x smaller than float32.

    Retrieval then uses Hamming distance (a XOR + popcount, effectively free).
    The standard recipe is a two-stage funnel: binary search for the top ~200,
    then rescore those with float32 vectors. Recall typically stays above 95%
    of the full-precision baseline.
    """
    return (vectors > 0).astype(np.uint8)


def hamming(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.count_nonzero(a != b, axis=-1)


def int8_quantize(vectors: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Scalar quantisation: linear map from [min, max] onto [-128, 127].
    4x smaller with near-zero recall loss -- the best default trade-off, and
    what most vector DBs offer as a one-line config flag."""
    lo, hi = float(vectors.min()), float(vectors.max())
    scale = (hi - lo) / 255.0
    quantized = np.round((vectors - lo) / scale - 128).astype(np.int8)
    return quantized, scale, lo


if __name__ == "__main__":
    print("=== 1. prefix effect (asymmetric models) ===")
    for label, scores in [("with prefix", with_prefixes(QUERY, PASSAGES)),
                          ("no prefix ", without_prefixes(QUERY, PASSAGES))]:
        ranking = [PASSAGES[i][:44] for i in np.argsort(-scores)]
        print(f"  {label}: {np.round(scores, 4)}  best={ranking[0]!r}")

    vectors = embedder().encode(PASSAGES, normalize_embeddings=True, show_progress_bar=False)
    query_vector = embedder().encode([QUERY], normalize_embeddings=True)[0]

    print("\n=== 2-3. metrics on unit vectors (same ranking, different scale) ===")
    print(f"  {metrics_demo(query_vector, vectors[0])}")

    print("\n=== 4. matryoshka truncation ===")
    full = vectors @ query_vector
    for dims in (vectors.shape[1], 256, 128, 64):
        cut_scores = truncate(vectors, dims) @ truncate(query_vector[None, :], dims)[0]
        agree = "same" if np.argmax(cut_scores) == np.argmax(full) else "DIFFERENT"
        print(f"  {dims:>4} dims -> top-1 {agree}, scores {np.round(cut_scores, 3)}")

    print("\n=== 5. quantisation ===")
    binary_docs, binary_query = binary_quantize(vectors), binary_quantize(query_vector)
    print(f"  float32 ranking: {list(np.argsort(-full))}")
    print(f"  binary  ranking: {list(np.argsort(hamming(binary_docs, binary_query)))}")
    quantized, scale, lo = int8_quantize(vectors)
    print(f"  int8: {vectors.nbytes} -> {quantized.nbytes} bytes "
          f"({vectors.nbytes / quantized.nbytes:.0f}x smaller)")
