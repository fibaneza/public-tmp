"""Hierarchical RAG: route at the summary level, retrieve at the chunk level.

Flat RAG treats a 50,000-chunk corpus as one undifferentiated pool. Two things
break at that scale:

  * *Dilution* -- with enough near-duplicate chunks (every runbook says "check
    the dashboard"), the top-k fills with plausible-but-wrong neighbours from
    documents that were never relevant.
  * *Aboutness* -- "which system owns settlement?" is a question about a
    document, not about any sentence inside it. No chunk contains the answer.

Hierarchical RAG builds a tree and searches it top-down:

    level 2   corpus summaries      "which collection could answer this?"
    level 1   document summaries    "which document could answer this?"
    level 0   chunks                "which passage answers this?"

Each level is its own embedded index. A query descends: match summaries, keep
the top branches, and only search chunks *within* those branches. The chunk
search now runs over hundreds of candidates instead of tens of thousands, so
precision rises and latency falls at the same time.

Related designs:
  * RAPTOR (Sarthi et al., 2024) builds the tree bottom-up by recursively
    clustering and summarising chunks, then retrieves from *all* levels at once
    -- so an abstract question hits a high-level summary node and a specific one
    hits a leaf.
  * GraphRAG (Microsoft) replaces the summary tree with an entity graph plus
    community summaries -- better for "what are the themes across everything?".

Run:  python hierarchical_rag.py "who owns the settlement flow and how is it corrected?"
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

import numpy as np

from _common import Chunk, embedder


@dataclass
class Node:
    """One node of the hierarchy. Leaves hold verbatim text; internal nodes hold
    a summary that is embedded in place of their children."""

    id: str
    text: str                      # summary for internal nodes, source text for leaves
    level: int                     # 0 = chunk, 1 = document, 2 = collection
    children: list["Node"] = field(default_factory=list)
    doc_title: str = ""
    page: int = 0

    def leaves(self) -> list["Node"]:
        return [self] if not self.children else [n for c in self.children for n in c.leaves()]


def build_tree() -> Node:
    """Hand-built here for clarity. In production level-1 and level-2 summaries
    are LLM-generated at ingest time -- one call per document, cached, and
    regenerated only when the document's content hash changes."""
    ledger = Node(
        id="d-arch", level=1, doc_title="Payments Architecture",
        text="Internal architecture of the payments platform: the append-only ledger "
             "service, idempotency guarantees on write endpoints, and settlement.",
        children=[
            Node("c-arch-1", "The ledger service is append-only. Every authorisation, "
                 "capture and refund is an immutable entry; corrections are compensating "
                 "entries, never updates.", 0, doc_title="Payments Architecture", page=7),
            Node("c-arch-2", "Settlement is owned by the treasury team. Batches close at "
                 "22:00 UTC and are reconciled against the ledger the next morning.", 0,
                 doc_title="Payments Architecture", page=9),
        ],
    )
    policy = Node(
        id="d-policy", level=1, doc_title="Refund Policy",
        text="Customer-facing refund rules: the 14-day eligibility window, how approved "
             "refunds are paid back, and exceptions for digital goods.",
        children=[
            Node("c-pol-1", "Customers may request a refund within 14 days of purchase.", 0,
                 doc_title="Refund Policy", page=1),
            Node("c-pol-2", "Approved refunds are returned to the original payment method; "
                 "card refunds settle in three to five business days.", 0,
                 doc_title="Refund Policy", page=2),
        ],
    )
    return Node(
        id="root", level=2, text="All payments documentation.",
        children=[ledger, policy],
    )


class HierarchicalRetriever:
    def __init__(self, root: Node):
        self.root = root
        self._index: dict[str, np.ndarray] = {}
        self._embed_level(root)

    def _embed_level(self, node: Node) -> None:
        """One embedding matrix per parent, so a descent step only ever compares
        against that parent's children."""
        if node.children:
            texts = [c.text for c in node.children]
            self._index[node.id] = embedder().encode(
                texts, normalize_embeddings=True, show_progress_bar=False
            )
            for child in node.children:
                self._embed_level(child)

    def _rank_children(self, node: Node, q: np.ndarray, top: int) -> list[tuple[Node, float]]:
        scores = self._index[node.id] @ q
        return [(node.children[i], float(scores[i])) for i in np.argsort(-scores)[:top]]

    def search(self, query: str, branches: int = 1, k: int = 3, trace: bool = True):
        """Beam search down the tree.

        `branches` is the beam width. Width 1 is fastest but a routing mistake at
        level 1 is unrecoverable -- the correct chunk is never even scored. Width
        2-3 is the usual safety/latency compromise; measure it with recall@k on
        your own eval set rather than guessing.
        """
        q = embedder().encode([query], normalize_embeddings=True)[0]

        frontier = [self.root]
        while any(n.children and n.children[0].children for n in frontier):
            scored = [(child, s) for n in frontier for child, s in
                      self._rank_children(n, q, branches)]
            scored.sort(key=lambda cs: -cs[1])
            frontier = [c for c, _ in scored[:branches]]
            if trace:
                print(f"  level {frontier[0].level} -> "
                      f"{[(n.doc_title or n.id) for n in frontier]}")

        # Final descent: score the leaves of the surviving branches only.
        leaves = [leaf for node in frontier for leaf in node.leaves()]
        matrix = embedder().encode([leaf.text for leaf in leaves],
                                   normalize_embeddings=True, show_progress_bar=False)
        scores = matrix @ q
        return [(leaves[i], float(scores[i])) for i in np.argsort(-scores)[:k]]


if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else (
        "who owns the settlement flow and how are mistakes corrected?"
    )
    print(f"query: {query}\n\nrouting:")
    for leaf, score in HierarchicalRetriever(build_tree()).search(query, branches=1, k=3):
        print(f"\n  {score:+.4f} [{leaf.doc_title} p.{leaf.page}]\n    {leaf.text[:110]}...")
