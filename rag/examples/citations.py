"""Source citations: making an answer auditable, and verifying it after the fact.

An uncited RAG answer is unfalsifiable -- the user cannot tell a grounded claim
from a hallucination without re-reading the corpus. Three mechanisms, in
increasing order of trustworthiness:

  1. *Prompted markers* -- number the context blocks, tell the model to write
     [1] after each claim. Cheap, works well, but the model can attach the wrong
     number or cite a chunk it did not use.
  2. *Structured output* -- make the model emit JSON: a list of claims, each
     with its supporting chunk ids and a verbatim quote. Now the citation is
     data you can validate, not prose you have to parse.
  3. *Post-hoc verification* -- check that every quoted span actually occurs in
     the chunk it points at, and (optionally) run an NLI/LLM entailment check
     that the chunk supports the claim. This is the only mechanism that catches
     a confidently wrong citation.

Ship 2 + 3 together: 2 gives you something to check, 3 does the checking.

Run:  python citations.py "how long do I have to request a refund and how is it paid back?"
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass

from _common import CORPUS, Chunk, complete
from hybrid_search import HybridRetriever

CITED_ANSWER_PROMPT = """Answer the question using ONLY the numbered sources below.

Rules:
- Every factual sentence must cite at least one source id.
- `quote` must be copied VERBATIM from the cited source -- never paraphrased.
- If the sources do not answer the question, return an empty "claims" list and
  explain why in "shortfall".

Sources:
{context}

Question: {question}

Respond with JSON only, in exactly this shape:
{{"claims": [{{"text": "...", "source_ids": ["c1"], "quote": "..."}}], "shortfall": null}}"""


@dataclass
class Claim:
    text: str
    source_ids: list[str]
    quote: str
    verified: bool = False
    reason: str = ""


def _numbered_context(chunks: list[Chunk]) -> str:
    """Expose the stable chunk id, not a positional index. Position changes with
    every retrieval; the id is what you can resolve back to a document, page and
    a deep link in the UI."""
    return "\n\n".join(
        f"<source id=\"{c.id}\" doc=\"{c.doc_title}\" page=\"{c.page}\">\n{c.text}\n</source>"
        for c in chunks
    )


def _normalise(text: str) -> str:
    """Collapse whitespace and case so a quote that differs only in line wrapping
    still matches. Do NOT strip punctuation -- "not refundable" vs "refundable"
    is exactly the difference you are trying to catch."""
    return re.sub(r"\s+", " ", text).strip().lower()


def answer_with_citations(question: str, chunks: list[Chunk]) -> tuple[list[Claim], str | None]:
    raw = complete(CITED_ANSWER_PROMPT.format(
        context=_numbered_context(chunks), question=question
    ))
    # Models sometimes wrap JSON in a fence despite instructions.
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    payload = json.loads(raw)
    claims = [Claim(**{k: c[k] for k in ("text", "source_ids", "quote")})
              for c in payload.get("claims", [])]
    return claims, payload.get("shortfall")


def verify(claims: list[Claim], chunks: list[Chunk]) -> list[Claim]:
    """Mechanism 3: the citation must resolve, and the quote must exist.

    This catches the two failure modes that prompting alone cannot: a fabricated
    source id, and a quote the model smoothed out while copying (which usually
    means it was reconstructing from memory rather than reading)."""
    by_id = {c.id: c for c in chunks}
    for claim in claims:
        unknown = [sid for sid in claim.source_ids if sid not in by_id]
        if unknown:
            claim.reason = f"cites unknown source(s): {unknown}"
            continue
        if not claim.quote.strip():
            claim.reason = "no quote provided"
            continue
        haystacks = [_normalise(by_id[sid].text) for sid in claim.source_ids]
        if any(_normalise(claim.quote) in hay for hay in haystacks):
            claim.verified = True
        else:
            claim.reason = "quote not found verbatim in the cited source"
    return claims


def render(claims: list[Claim], chunks: list[Chunk]) -> str:
    """Render only verified claims to the user, with a resolvable footnote.

    An unverified claim is not automatically a lie, but it is not evidence
    either -- surface it separately or drop it, never inline as though it were
    grounded."""
    by_id = {c.id: c for c in chunks}
    lines, footnotes = [], {}
    for claim in claims:
        if not claim.verified:
            continue
        markers = []
        for sid in claim.source_ids:
            if sid not in footnotes:
                footnotes[sid] = len(footnotes) + 1
            markers.append(f"[{footnotes[sid]}]")
        lines.append(f"{claim.text} {''.join(markers)}")

    body = " ".join(lines) or "(no verified claims -- the sources do not answer this)"
    refs = "\n".join(
        f"[{n}] {by_id[sid].citation()} — \"{by_id[sid].text[:70]}...\""
        for sid, n in sorted(footnotes.items(), key=lambda kv: kv[1])
    )
    return f"{body}\n\nSources:\n{refs}" if refs else body


if __name__ == "__main__":
    question = sys.argv[1] if len(sys.argv) > 1 else (
        "how long do I have to request a refund and how is it paid back?"
    )
    retrieved = [c for c, _ in HybridRetriever(CORPUS).rrf(question, k=4)]

    claims, shortfall = answer_with_citations(question, retrieved)
    claims = verify(claims, retrieved)

    print("=== verification ===")
    for claim in claims:
        mark = "OK  " if claim.verified else "FAIL"
        print(f"  [{mark}] {claim.text[:60]}... -> {claim.source_ids} {claim.reason}")
    if shortfall:
        print(f"  shortfall reported by model: {shortfall}")

    print("\n=== rendered answer ===")
    print(render(claims, retrieved))
