"""Shared helpers for the RAG examples in this folder.

Every other example imports from here so the individual files stay focused on
the one technique they are demonstrating instead of re-declaring a corpus, an
embedding model and an LLM client each time.

Nothing here requires a network call at import time -- the models are created
lazily so you can read/import the modules even without the weights downloaded.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass, field
from typing import Iterable


# ---------------------------------------------------------------------------
# A tiny, deliberately adversarial corpus
# ---------------------------------------------------------------------------
# It contains near-duplicates, an acronym that only appears in one chunk
# ("PIT"), and a numeric fact ("14 days"). Those are exactly the cases where
# pure vector search under-performs and BM25 or reranking earns its keep.

@dataclass
class Chunk:
    """One retrievable unit plus the metadata needed for citations."""

    id: str
    text: str
    doc_id: str
    doc_title: str
    page: int
    section: str = ""
    # Filled in later by the parent-child / hierarchical examples.
    parent_id: str | None = None
    metadata: dict = field(default_factory=dict)

    def citation(self) -> str:
        """Human-readable provenance string, e.g. [Runbook p.3 §Rollback]."""
        section = f" §{self.section}" if self.section else ""
        return f"[{self.doc_title} p.{self.page}{section}]"


CORPUS: list[Chunk] = [
    Chunk(
        id="c1",
        doc_id="runbook",
        doc_title="Payments Runbook",
        page=3,
        section="Rollback",
        text=(
            "To roll back a payments deployment, run `payctl rollback --to <release>`. "
            "The command drains traffic from the new pods before promoting the previous "
            "release, so no in-flight authorisation is lost."
        ),
    ),
    Chunk(
        id="c2",
        doc_id="runbook",
        doc_title="Payments Runbook",
        page=4,
        section="Incident response",
        text=(
            "During a Sev-1 the on-call engineer opens a PIT (Payments Incident Thread) "
            "in the #payments-incident channel. The PIT is the single source of truth "
            "for the timeline and must be linked from the post-mortem."
        ),
    ),
    Chunk(
        id="c3",
        doc_id="policy",
        doc_title="Refund Policy",
        page=1,
        section="Eligibility",
        text=(
            "Customers may request a refund within 14 days of purchase. Requests after "
            "14 days are handled case by case by the support lead."
        ),
    ),
    Chunk(
        id="c4",
        doc_id="policy",
        doc_title="Refund Policy",
        page=2,
        section="Processing",
        text=(
            "Approved refunds are returned to the original payment method. Card refunds "
            "settle in three to five business days; bank transfers can take longer."
        ),
    ),
    Chunk(
        id="c5",
        doc_id="architecture",
        doc_title="Payments Architecture",
        page=7,
        section="Ledger",
        text=(
            "The ledger service is append-only. Every authorisation, capture and refund "
            "is written as an immutable entry; balances are derived by folding the entries."
        ),
    ),
    Chunk(
        id="c6",
        doc_id="architecture",
        doc_title="Payments Architecture",
        page=8,
        section="Idempotency",
        text=(
            "Every write endpoint accepts an Idempotency-Key header. Keys are retained "
            "for 24 hours, after which a replayed request is treated as a new one."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Lazily constructed models
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def embedder(model_name: str = "BAAI/bge-small-en-v1.5"):
    """Return a cached SentenceTransformer bi-encoder.

    A bi-encoder embeds query and document *independently*, which is what makes
    pre-computing the corpus (and therefore ANN search) possible.
    """
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name)


@functools.lru_cache(maxsize=None)
def cross_encoder(model_name: str = "BAAI/bge-reranker-v2-m3"):
    """Return a cached cross-encoder reranker.

    A cross-encoder sees query and document *together* in one forward pass, so
    it cannot be pre-computed -- it only runs on the shortlist.
    """
    from sentence_transformers import CrossEncoder

    return CrossEncoder(model_name)


@functools.lru_cache(maxsize=None)
def llm():
    """Return a cached Anthropic client. Requires ANTHROPIC_API_KEY."""
    import anthropic

    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


DEFAULT_MODEL = "claude-sonnet-5"


def complete(prompt: str, *, system: str | None = None, max_tokens: int = 1024) -> str:
    """One-shot completion helper used by the generation-side examples."""
    kwargs = {"system": system} if system else {}
    message = llm().messages.create(
        model=DEFAULT_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        **kwargs,
    )
    return "".join(block.text for block in message.content if block.type == "text")


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def format_context(chunks: Iterable[Chunk]) -> str:
    """Render chunks for a prompt with stable, quotable source markers."""
    return "\n\n".join(
        f"[{i}] {c.citation()}\n{c.text}" for i, c in enumerate(chunks, start=1)
    )


def show(title: str, chunks: Iterable[Chunk], scores: Iterable[float] | None = None) -> None:
    """Pretty-print a ranked list so the examples are readable when run."""
    print(f"\n=== {title} ===")
    scores = list(scores) if scores is not None else []
    for rank, chunk in enumerate(chunks, start=1):
        score = f"{scores[rank - 1]:+.4f}  " if rank <= len(scores) else ""
        print(f"{rank:>2}. {score}{chunk.id} {chunk.citation()} {chunk.text[:72]}...")
