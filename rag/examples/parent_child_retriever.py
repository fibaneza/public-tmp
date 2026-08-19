"""Parent-Child retrieval (a.k.a. small-to-big, ParentDocumentRetriever).

Chunk size forces a trade-off that has no good single answer:

  * Small chunks embed *precisely*. One idea per vector, so cosine similarity
    means something. But the retrieved text is too thin to answer from -- the
    pronoun's antecedent, the table header, the preceding condition are all in
    the neighbouring chunk.
  * Large chunks carry *context*. But their embedding is the average of five
    topics, so it is close to everything and decisive about nothing.

Parent-Child refuses the trade-off: index the small chunks, return the big ones.

    search over CHILD embeddings  ->  dedupe to PARENT ids  ->  feed PARENT text

Two common shapes:
  * parent = section, child = paragraph   (this file)
  * parent = paragraph, child = sentence  ("sentence-window retrieval", where
    the window is n sentences either side of the hit rather than a stored parent)

Run:  python parent_child_retriever.py "how are balances calculated?"
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import numpy as np

from _common import Chunk, embedder


@dataclass
class Parent:
    """The unit sent to the LLM. Never embedded, only stored by id."""

    id: str
    text: str
    doc_title: str
    page: int
    section: str


# --- a two-level corpus ----------------------------------------------------
PARENTS = [
    Parent(
        id="p1", doc_title="Payments Architecture", page=7, section="Ledger",
        text=(
            "The ledger service is append-only. Every authorisation, capture and refund "
            "is written as an immutable entry. Entries are never updated or deleted; a "
            "correction is a new compensating entry. Balances are derived by folding the "
            "entries for an account in sequence, which means any historical balance can be "
            "reconstructed by replaying up to a point in time."
        ),
    ),
    Parent(
        id="p2", doc_title="Payments Architecture", page=8, section="Idempotency",
        text=(
            "Every write endpoint accepts an Idempotency-Key header. The gateway stores the "
            "key together with the response body. A replay within the retention window "
            "returns the stored response without re-executing the write. Keys are retained "
            "for 24 hours, after which a replayed request is treated as a new one."
        ),
    ),
]


def split_into_children(parent: Parent, max_chars: int = 120) -> list[Chunk]:
    """Split a parent into sentence-ish children that point back at it.

    In production use a token-aware recursive splitter; the point here is only
    the `parent_id` back-reference.
    """
    import re

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", parent.text) if s.strip()]
    children, buffer = [], ""
    for sentence in sentences:
        if buffer and len(buffer) + len(sentence) > max_chars:
            children.append(buffer)
            buffer = sentence
        else:
            buffer = f"{buffer} {sentence}".strip()
    if buffer:
        children.append(buffer)

    return [
        Chunk(
            id=f"{parent.id}-c{i}", text=text, doc_id=parent.id,
            doc_title=parent.doc_title, page=parent.page, section=parent.section,
            parent_id=parent.id,
        )
        for i, text in enumerate(children)
    ]


class ParentChildRetriever:
    def __init__(self, parents: list[Parent]):
        self.parents = {p.id: p for p in parents}
        self.children: list[Chunk] = [c for p in parents for c in split_into_children(p)]
        # Only the children ever get embedded -- the parent store is a plain
        # key-value lookup (a dict here, Redis or a docstore in production).
        self.matrix = embedder().encode(
            [c.text for c in self.children], normalize_embeddings=True, show_progress_bar=False
        )

    def search(self, query: str, k_children: int = 4, k_parents: int = 2):
        q = embedder().encode([query], normalize_embeddings=True)[0]
        scores = self.matrix @ q

        # Retrieve generously at child level, then collapse. Several children of
        # the same parent hitting is a *strength* signal, so keep the best score
        # per parent rather than counting duplicates.
        best_per_parent: dict[str, float] = {}
        for idx in np.argsort(-scores)[: k_children * 3]:
            child = self.children[idx]
            pid = child.parent_id
            best_per_parent[pid] = max(best_per_parent.get(pid, -1.0), float(scores[idx]))

        ranked = sorted(best_per_parent.items(), key=lambda kv: -kv[1])[:k_parents]
        return [(self.parents[pid], score) for pid, score in ranked]


if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else "how are balances calculated?"
    retriever = ParentChildRetriever(PARENTS)

    print("=== child chunks that were indexed ===")
    for child in retriever.children:
        print(f"  {child.id}: {child.text[:70]}...")

    print(f"\n=== parents returned for: {query!r} ===")
    for parent, score in retriever.search(query):
        print(f"\n{score:+.4f}  [{parent.doc_title} p.{parent.page} §{parent.section}]")
        print(f"  {parent.text}")
