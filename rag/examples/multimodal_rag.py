"""Multimodal RAG: retrieving figures, charts and tables, not just prose.

Implements strategy ① from RAG_Multimodal.adoc -- *text-proxy indexing* -- because
it is the one that drops into an existing text pipeline without replacing it:

    extract figures -> VLM writes a description -> embed the DESCRIPTION
    -> retrieve -> pass the IMAGE (not the description) to a vision model

The two rules that decide whether this works, both enforced below:

  1. The describer gets CONTEXT -- the figure's caption and the paragraph that
     references it. A description written from pixels alone says "a bar chart
     with four bars"; one written with context says "quarterly FY2023 revenue,
     growing except for a Q3 dip". Only the second answers a real question.
  2. Generation reads the IMAGE, never the description. The description is a
     retrieval device, like a HyDE hypothesis. Answering from it compounds any
     error it contains, and citing it would attribute a claim to model output
     rather than to the source document.

Strategies ② (unified CLIP-style embedding) and ③ (ColPali page images) are
sketched at the bottom for comparison.

Run:  python multimodal_rag.py report.pdf "which quarter had the revenue dip?"
"""

from __future__ import annotations

import base64
import mimetypes
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from _common import DEFAULT_MODEL, embedder, llm


# ---------------------------------------------------------------------------
# The indexed unit
# ---------------------------------------------------------------------------

@dataclass
class Figure:
    """One figure, plus everything needed to retrieve, render and cite it."""

    id: str
    image_path: Path            # the evidence -- what the VLM answers from
    doc_id: str
    doc_title: str
    page: int
    caption: str = ""           # from the document, verbatim
    surrounding_text: str = ""  # the paragraph that references it
    kind: str = "figure"        # figure | table | chart | diagram | screenshot
    description: str = ""       # VLM-written -- NEVER shown as source text
    bbox: tuple[float, ...] | None = None   # for highlight-in-place in the UI

    @property
    def embed_text(self) -> str:
        """What actually goes into the index: caption + description together.

        The caption is verbatim document text and usually contains the exact
        nouns a user will search for, so it carries the lexical/BM25 weight;
        the description carries the semantic content the caption omits.
        """
        parts = [f"{self.doc_title}, page {self.page}"]
        if self.caption:
            parts.append(self.caption)
        parts.append(self.description)
        return "\n".join(parts)

    def citation(self) -> str:
        return f"[{self.doc_title} p.{self.page} — {self.caption or self.kind}]"


# ---------------------------------------------------------------------------
# Ingest: extract figures with provenance
# ---------------------------------------------------------------------------

def extract_figures(pdf_path: str, out_dir: Path) -> list[Figure]:
    """Pull figures and tables out of a PDF with page numbers intact.

    Docling is used because it gives *provenance* -- page number and bounding
    box -- which a plain image extractor does not. Without provenance you can
    retrieve a figure but not cite it, and an uncitable figure is barely better
    than no figure.
    """
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    options = PdfPipelineOptions()
    options.generate_picture_images = True   # off by default -- we need the pixels
    options.generate_table_images = True
    options.images_scale = 2.0               # 2x render: small text stays legible

    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )
    document = converter.convert(pdf_path).document
    out_dir.mkdir(parents=True, exist_ok=True)

    figures: list[Figure] = []
    for i, item in enumerate(list(document.pictures) + list(document.tables)):
        image = item.get_image(document)
        if image is None:
            continue
        path = out_dir / f"fig_{i:03d}.png"
        image.save(path)

        prov = item.prov[0] if getattr(item, "prov", None) else None
        figures.append(Figure(
            id=f"fig-{i}",
            image_path=path,
            doc_id=Path(pdf_path).stem,
            doc_title=Path(pdf_path).stem.replace("_", " ").title(),
            page=getattr(prov, "page_no", 0) if prov else 0,
            caption=item.caption_text(document) or "",
            kind="table" if item in document.tables else "figure",
            bbox=tuple(getattr(prov, "bbox", ()) or ()) if prov else None,
        ))
    return figures


# ---------------------------------------------------------------------------
# Ingest: describe each figure with a VLM
# ---------------------------------------------------------------------------

DESCRIBE_PROMPT = """Describe this {kind} so that someone searching a document
index can find it from a natural-language question.

Caption from the document: {caption}
Surrounding text: {surrounding}

Rules:
- State what the {kind} SHOWS, not what it looks like. No colours, no styling.
- For a chart: name the axes and units, then state the trend, the maximum, the
  minimum, and any anomaly. Transcribe the values if they are readable.
- For a table: transcribe it as a Markdown table, headers included.
- For a diagram: enumerate the components and the relationships between them
  (what calls what, what flows where).
- Transcribe any text visible in the image verbatim.
- Do not speculate about anything not visible.

Description:"""


def _image_block(path: Path) -> dict:
    """Anthropic image content block. Images are billed by resolution, so
    downscale to the smallest size at which the text stays legible before
    sending -- 2x render, then resize, is a good default."""
    media_type = mimetypes.guess_type(path)[0] or "image/png"
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(path.read_bytes()).decode(),
        },
    }


def describe(figure: Figure) -> str:
    message = llm().messages.create(
        model=DEFAULT_MODEL,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                _image_block(figure.image_path),
                {"type": "text", "text": DESCRIBE_PROMPT.format(
                    kind=figure.kind,
                    caption=figure.caption or "(none)",
                    surrounding=figure.surrounding_text or "(none)",
                )},
            ],
        }],
    )
    return "".join(b.text for b in message.content if b.type == "text").strip()


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

@dataclass
class MultimodalRetriever:
    """Figures indexed by their descriptions, in an ordinary text vector space.

    Because the index holds text, everything already built keeps working:
    hybrid search, cross-encoder reranking, metadata filters. That is the whole
    argument for this strategy over a separate multimodal index.
    """

    figures: list[Figure]
    matrix: np.ndarray = field(init=False)

    def __post_init__(self):
        self.matrix = embedder().encode(
            [f.embed_text for f in self.figures],
            normalize_embeddings=True, show_progress_bar=False,
        )

    def search(self, query: str, k: int = 3) -> list[tuple[Figure, float]]:
        q = embedder().encode([query], normalize_embeddings=True)[0]
        scores = self.matrix @ q
        return [(self.figures[i], float(scores[i])) for i in np.argsort(-scores)[:k]]


# ---------------------------------------------------------------------------
# Generation: answer from the images
# ---------------------------------------------------------------------------

ANSWER_PROMPT = """Answer the question using only the attached figures.
Cite the figure you used by its bracketed marker after each claim.
If the figures do not contain the answer, say so plainly.

Question: {question}"""


def answer(question: str, hits: list[Figure]) -> str:
    """Interleave images and their markers, then ask the vision model.

    Note what is NOT sent: the VLM-written descriptions. They did their job at
    retrieval time. Sending them invites the model to answer from second-hand
    text when the primary source is right there.
    """
    content: list[dict] = []
    for figure in hits:
        content.append({"type": "text", "text": figure.citation()})
        content.append(_image_block(figure.image_path))
    content.append({"type": "text", "text": ANSWER_PROMPT.format(question=question)})

    message = llm().messages.create(
        model=DEFAULT_MODEL, max_tokens=1024,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(b.text for b in message.content if b.type == "text")


# ---------------------------------------------------------------------------
# Strategies ② and ③, for comparison
# ---------------------------------------------------------------------------

def unified_embedding_sketch():
    """② One vector space for text and images (CLIP / SigLIP / voyage-multimodal).

    Strong for photo libraries and product catalogues. Weak for documents: the
    CLIP family is trained on short alt-text captions, so it matches "a photo of
    a dog" well and "the third-quarter revenue dip" badly. Also watch the
    modality gap -- image vectors cluster with image vectors, so one cosine
    threshold does not serve both. Retrieving each modality separately and
    fusing with RRF sidesteps that entirely.
    """
    return """
    from sentence_transformers import SentenceTransformer
    from PIL import Image

    model = SentenceTransformer("clip-ViT-B-32")
    image_vectors = model.encode([Image.open(p) for p in image_paths],
                                 normalize_embeddings=True)
    query_vector  = model.encode(["quarterly revenue chart"],
                                 normalize_embeddings=True)[0]
    scores = image_vectors @ query_vector
    """


def colpali_sketch():
    """③ Embed page IMAGES with late interaction -- no parsing, no chunking, no OCR.

    Highest quality on scans, forms and dense reports. Costs ~1030 patch vectors
    per page (~100x storage), needs a GPU, retrieves whole pages rather than
    passages, and requires a store that supports multivector/late-interaction
    indexing (Vespa, Qdrant multivectors).
    """
    return """
    from byaldi import RAGMultiModalModel

    rag = RAGMultiModalModel.from_pretrained("vidore/colpali-v1.2")
    rag.index(input_path="report.pdf", index_name="report")
    results = rag.search("which quarter had the revenue dip?", k=3)
    # results carry page numbers and base64 page images -> feed to a VLM
    """


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit("usage: python multimodal_rag.py <file.pdf> [question]")

    pdf = sys.argv[1]
    question = sys.argv[2] if len(sys.argv) > 2 else "which quarter had the revenue dip?"

    figures = extract_figures(pdf, Path("./.figures"))
    print(f"extracted {len(figures)} figures/tables")

    for figure in figures:
        figure.description = describe(figure)
        print(f"  {figure.id} p.{figure.page} [{figure.kind}] "
              f"{figure.description[:90]}...")

    print(f"\nquery: {question}")
    hits = MultimodalRetriever(figures).search(question, k=3)
    for figure, score in hits:
        print(f"  {score:+.4f} {figure.citation()}")

    print(f"\n{answer(question, [f for f, _ in hits])}")
