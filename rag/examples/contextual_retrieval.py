"""Contextual Retrieval (Anthropic, 2024): give each chunk its context back.

The core failure of any chunking scheme is that a chunk is torn out of the
document that made it meaningful. Consider:

    "The company's revenue grew by 3% over the previous quarter."

Which company? Which quarter? The chunk cannot be retrieved by "ACME Q2 2023
revenue growth" because none of those tokens are in it, and if it is retrieved
the model cannot safely attribute it.

Contextual Retrieval fixes this at *index* time: before embedding, ask an LLM
to write a one-or-two sentence situating preamble using the whole document, and
prepend it to the chunk.

    "This chunk is from ACME Corp's Q2 2023 SEC filing; the previous quarter's
     revenue was $314M. The company's revenue grew by 3% over the previous quarter."

Anthropic's reported results on their benchmark suite:
  * contextual embeddings alone           -> ~35% fewer failed retrievals
  * + contextual BM25                     -> ~49% fewer
  * + reranking                           -> ~67% fewer

The cost objection ("an LLM call per chunk") is answered by prompt caching: the
whole document goes in the cached prefix once, and each chunk call only pays for
the chunk. That puts it around a dollar per million tokens of corpus, one time.

Trade-offs: index-time cost and a full re-index when you change the prompt; and
the preamble is model-written, so it must never be quoted back to the user as
source text -- store it in a separate field from the verbatim chunk.

Run:  python contextual_retrieval.py
"""

from __future__ import annotations

from dataclasses import dataclass

import anthropic

from _common import DEFAULT_MODEL, llm

DOCUMENT = """ACME Corp -- Quarterly Report, Q2 2023

ACME Corp is a payments infrastructure company headquartered in Dublin. This
report covers the three months ending 30 June 2023. Revenue in Q1 2023 was
$314 million.

Financial results
The company's revenue grew by 3% over the previous quarter. Gross margin was
flat at 61%. Operating expenses rose 8%, driven mainly by headcount in the
fraud-detection team.

Outlook
Management expects mid-single-digit growth in Q3, with margin pressure from the
migration of the ledger service to a new provider.
"""

CHUNKS = [
    "The company's revenue grew by 3% over the previous quarter. Gross margin was "
    "flat at 61%. Operating expenses rose 8%, driven mainly by headcount in the "
    "fraud-detection team.",
    "Management expects mid-single-digit growth in Q3, with margin pressure from "
    "the migration of the ledger service to a new provider.",
]

CONTEXT_PROMPT = """Here is the chunk we want to situate within the whole document:
<chunk>
{chunk}
</chunk>

Give a short, succinct context (1-2 sentences) to situate this chunk within the
overall document, for the purposes of improving search retrieval of the chunk.
Resolve every pronoun, relative date and unnamed entity the chunk relies on.
Answer only with the succinct context and nothing else."""


@dataclass
class ContextualChunk:
    raw: str            # verbatim source text -- this is what you quote and cite
    context: str        # LLM-written preamble -- never shown as source
    embed_text: str     # context + raw -- this is what you embed AND what BM25 indexes


def contextualize(document: str, chunks: list[str]) -> list[ContextualChunk]:
    """One call per chunk, with the document in a cached prefix.

    `cache_control` marks the document block as cacheable: the first call writes
    the cache, every subsequent chunk in the same batch reads it at ~10% of the
    input price. Without this the pattern costs (n_chunks x document_tokens),
    which is what makes people dismiss it as too expensive.
    """
    client = llm()
    out: list[ContextualChunk] = []

    for chunk in chunks:
        message = client.messages.create(
            model=DEFAULT_MODEL,
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"<document>\n{document}\n</document>",
                        "cache_control": {"type": "ephemeral"},
                    },
                    {"type": "text", "text": CONTEXT_PROMPT.format(chunk=chunk)},
                ],
            }],
        )
        context = "".join(b.text for b in message.content if b.type == "text").strip()
        out.append(ContextualChunk(
            raw=chunk,
            context=context,
            # Prepending, not replacing: the verbatim text must survive so exact
            # lexical matches (BM25) and quoting still work.
            embed_text=f"{context}\n\n{chunk}",
        ))
    return out


if __name__ == "__main__":
    try:
        for contextual in contextualize(DOCUMENT, CHUNKS):
            print("=" * 70)
            print(f"raw     : {contextual.raw[:80]}...")
            print(f"context : {contextual.context}")
            print(f"embedded: {contextual.embed_text[:160]}...")
    except (KeyError, anthropic.AnthropicError) as exc:
        print(f"needs ANTHROPIC_API_KEY: {exc}")
