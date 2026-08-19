"""Structure-aware chunking with Docling: read the PDF's layout, not its bytes.

A PDF has no paragraphs. It is a bag of positioned glyphs. `pypdf.extract_text()`
walks them in storage order, which produces:

  * two-column papers interleaved line by line into nonsense
  * table cells emitted as a flat run of numbers with no header attached
  * headers, footers and page numbers spliced into the middle of sentences
  * headings indistinguishable from body text, so no hierarchy survives

Chunk that stream and every downstream stage inherits the damage -- the
embedding encodes garbled text, the reranker faithfully ranks garbled text, and
the citation points at a page whose content was never really there.

Docling runs layout analysis (reading order, headings, tables, figures, code
blocks) and emits a `DoclingDocument` -- a *tree*, not a string. Its
`HybridChunker` then chunks along that tree: it never crosses a section
boundary, keeps a table with its caption and headers, and prefixes each chunk
with its heading breadcrumb before tokenising to the embedding model's limit.

Alternatives with the same goal: unstructured.io, LlamaParse, Marker, Azure
Document Intelligence.

Run:  python docling_structure_chunking.py path/to/document.pdf
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass
class ParsedChunk:
    """A structure-aware chunk: text plus the provenance needed to cite it."""

    text: str
    embed_text: str          # heading breadcrumb + text, what actually gets embedded
    headings: list[str]
    page: int | None
    kind: str                # "text" | "table" | "list" | "code" | "caption"
    doc_id: str


def naive_pdf_chunks(path: str, size: int = 800, overlap: int = 100) -> list[str]:
    """The baseline: flatten to a string, slice it. Shown so the difference is
    concrete when you diff the two outputs on your own PDF."""
    from pypdf import PdfReader

    text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    step = size - overlap
    return [text[i:i + size] for i in range(0, len(text), step)]


def docling_chunks(path: str, doc_id: str | None = None,
                   embed_model: str = "BAAI/bge-small-en-v1.5",
                   max_tokens: int = 512) -> list[ParsedChunk]:
    """Parse layout, then chunk along the document tree.

    HybridChunker is "hybrid" in that it combines two rules: structural
    boundaries (never merge across sections) and a tokenizer budget (split a
    section that exceeds the embedding model's window, merge sibling fragments
    that are far under it). That second half is what stops a document of
    one-line bullet points becoming 400 near-empty vectors.
    """
    from docling.document_converter import DocumentConverter
    from docling.chunking import HybridChunker
    from transformers import AutoTokenizer

    document = DocumentConverter().convert(path).document

    chunker = HybridChunker(
        tokenizer=AutoTokenizer.from_pretrained(embed_model),
        max_tokens=max_tokens,
        merge_peers=True,   # glue adjacent under-sized siblings back together
    )

    out: list[ParsedChunk] = []
    for chunk in chunker.chunk(document):
        meta = chunk.meta
        headings = list(getattr(meta, "headings", None) or [])

        page = None
        for item in getattr(meta, "doc_items", []) or []:
            for prov in getattr(item, "prov", []) or []:
                page = getattr(prov, "page_no", None)
                if page is not None:
                    break
            if page is not None:
                break

        kinds = {getattr(item, "label", "text") for item in getattr(meta, "doc_items", []) or []}
        kind = "table" if any("table" in str(k) for k in kinds) else "text"

        out.append(ParsedChunk(
            text=chunk.text,
            # contextualize() prepends the heading path -- this is the string to
            # embed, while `text` stays clean for display and quoting.
            embed_text=chunker.contextualize(chunk=chunk),
            headings=headings,
            page=page,
            kind=kind,
            doc_id=doc_id or path,
        ))
    return out


def export_markdown(path: str) -> str:
    """Docling can also serialise the whole tree to Markdown -- tables become
    real Markdown tables. Useful when you want to chunk with your own splitter
    but still want layout-correct input."""
    from docling.document_converter import DocumentConverter

    return DocumentConverter().convert(path).document.export_to_markdown()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit("usage: python docling_structure_chunking.py <file.pdf|.docx|.html|url>")

    source = sys.argv[1]

    print("=== naive (pypdf + fixed slicing) -- first 2 chunks ===")
    try:
        for chunk in naive_pdf_chunks(source)[:2]:
            print(f"  {chunk[:200]!r}\n")
    except Exception as exc:                     # non-PDF input, encrypted file, ...
        print(f"  (skipped: {exc})\n")

    print("=== docling (layout-aware) -- first 5 chunks ===")
    for chunk in docling_chunks(source)[:5]:
        breadcrumb = " > ".join(chunk.headings) or "(no heading)"
        print(f"  [{chunk.kind}] p.{chunk.page} {breadcrumb}")
        print(f"    {chunk.text[:160]!r}\n")
