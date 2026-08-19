"""Seven chunking strategies, side by side, on the same text.

Chunking is the highest-variance decision in a RAG system and the one most
often made by accident (`chunk_size=1000` from a tutorial). The chunk is the
unit of retrieval *and* the unit of citation, so its boundaries decide both
what can be found and what can be quoted.

Strategies, roughly in order of increasing sophistication:

  1. fixed_size        -- N characters, no overlap. The strawman.
  2. fixed_with_overlap-- N characters, M overlap. Cheap insurance against a
                          fact being cut in half. Standard default.
  3. token_based       -- size measured in tokens, not characters, so chunks
                          actually fit the embedding model's window.
  4. recursive         -- split on the largest natural separator that fits:
                          paragraphs, then lines, then sentences, then words.
  5. sentence_window   -- index one sentence, return it with n neighbours.
  6. semantic          -- cut where consecutive sentence embeddings diverge.
  7. structural        -- cut on the document's own headings/tables (see
                          docling_structure_chunking.py -- the real answer for PDFs).

Run:  python chunking_strategies.py
"""

from __future__ import annotations

import re

import numpy as np

from _common import embedder

SAMPLE = """# Refund Policy

Customers may request a refund within 14 days of purchase. Requests made after
14 days are handled case by case by the support lead.

## Processing

Approved refunds are returned to the original payment method. Card refunds
settle in three to five business days. Bank transfers can take longer,
especially across borders.

## Exceptions

Digital goods that have been downloaded are not refundable. Subscription
products are pro-rated from the cancellation date rather than refunded in full.
"""


def fixed_size(text: str, size: int = 200) -> list[str]:
    """Boundary-blind. A sentence -- and therefore a fact -- can be cut in half,
    leaving neither piece retrievable. Only acceptable for uniform machine text."""
    return [text[i:i + size] for i in range(0, len(text), size)]


def fixed_with_overlap(text: str, size: int = 200, overlap: int = 40) -> list[str]:
    """Overlap gives every boundary-straddling fact a second chance to live
    intact in one chunk. Cost: ~overlap/size extra storage and duplicate hits at
    query time (dedupe by id before building the prompt). 10-20% is typical."""
    step = size - overlap
    return [text[i:i + size] for i in range(0, len(text), step)]


def token_based(text: str, max_tokens: int = 64, overlap: int = 8) -> list[str]:
    """Characters are a proxy for tokens and a bad one: code and CJK text run
    2-4x more tokens per character than English prose. Size in tokens when the
    embedding model's window is what you are actually respecting."""
    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")
    ids = enc.encode(text)
    step = max_tokens - overlap
    return [enc.decode(ids[i:i + max_tokens]) for i in range(0, len(ids), step)]


def recursive(text: str, size: int = 220,
              separators: tuple[str, ...] = ("\n\n", "\n", ". ", " ")) -> list[str]:
    """Try to split on the most semantic separator available; fall back to finer
    ones only for pieces still over budget. This is what LangChain's
    RecursiveCharacterTextSplitter does and it is the right default for prose."""
    if len(text) <= size or not separators:
        return [text] if text.strip() else []

    head, *rest = separators
    parts, buffer = [], ""
    for piece in text.split(head):
        candidate = f"{buffer}{head}{piece}" if buffer else piece
        if len(candidate) <= size:
            buffer = candidate
        else:
            if buffer:
                parts.append(buffer)
            # The piece itself may still be too big -> recurse with a finer sep.
            parts.extend(recursive(piece, size, tuple(rest)) if len(piece) > size else [piece])
            buffer = ""
    if buffer:
        parts.append(buffer)
    return [p.strip() for p in parts if p.strip()]


def sentence_window(text: str, window: int = 1) -> list[dict]:
    """Index the sentence, return the neighbourhood.

    Retrieval precision comes from the single embedded sentence; answerability
    comes from the window handed to the LLM. Same insight as Parent-Child,
    expressed with an offset instead of a stored parent."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    out = []
    for i, sentence in enumerate(sentences):
        lo, hi = max(0, i - window), min(len(sentences), i + window + 1)
        out.append({"embed": sentence, "return": " ".join(sentences[lo:hi])})
    return out


def semantic(text: str, percentile: float = 80.0) -> list[str]:
    """Cut where the topic actually changes.

    Embed each sentence, measure cosine distance between consecutive sentences,
    and place a boundary wherever the distance spikes above a percentile of the
    document's own distribution. A relative threshold, not an absolute one --
    "unusually different *for this document*" travels across corpora, a hard
    0.3 does not."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if len(sentences) < 3:
        return sentences

    vectors = embedder().encode(sentences, normalize_embeddings=True, show_progress_bar=False)
    distances = [1.0 - float(vectors[i] @ vectors[i + 1]) for i in range(len(sentences) - 1)]
    threshold = np.percentile(distances, percentile)

    chunks, current = [], [sentences[0]]
    for i, distance in enumerate(distances):
        if distance > threshold:
            chunks.append(" ".join(current))
            current = []
        current.append(sentences[i + 1])
    if current:
        chunks.append(" ".join(current))
    return chunks


def markdown_structural(text: str) -> list[dict]:
    """Split on headings and carry the heading path into every chunk.

    Prefixing the breadcrumb ("Refund Policy > Processing") to the embedded text
    is a one-line change that measurably improves retrieval: it disambiguates
    chunks whose body text is generic ("settles in 3-5 business days" appears in
    a dozen documents)."""
    chunks, heading, buffer = [], "", []

    def flush():
        if buffer:
            body = " ".join(buffer).strip()
            if body:
                chunks.append({"heading": heading, "text": body,
                               "embed": f"{heading}\n{body}" if heading else body})

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            buffer.clear()
            heading = line.lstrip("#").strip()
        else:
            buffer.append(line)
    flush()
    return chunks


if __name__ == "__main__":
    for name, result in [
        ("fixed_size(200)", fixed_size(SAMPLE)),
        ("fixed_with_overlap(200/40)", fixed_with_overlap(SAMPLE)),
        ("recursive(220)", recursive(SAMPLE)),
        ("semantic(p80)", semantic(SAMPLE)),
    ]:
        print(f"\n{'=' * 70}\n{name} -> {len(result)} chunks")
        for i, chunk in enumerate(result):
            print(f"  [{i}] {chunk[:90]!r}")

    print(f"\n{'=' * 70}\nmarkdown_structural")
    for chunk in markdown_structural(SAMPLE):
        print(f"  §{chunk['heading']!r}: {chunk['text'][:70]!r}")

    print(f"\n{'=' * 70}\nsentence_window(1) -- first two entries")
    for entry in sentence_window(SAMPLE)[:2]:
        print(f"  embed : {entry['embed'][:60]!r}")
        print(f"  return: {entry['return'][:90]!r}\n")
