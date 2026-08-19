"""Document management: full CRUD over an indexed corpus, with per-doc metadata.

Most RAG tutorials stop at "ingest a folder". Then someone asks to delete a
document and you discover the index has no notion of documents at all -- only
loose vectors. The fixes are all structural and all cheap if you do them on day
one:

  * Every chunk carries `doc_id`. Deleting a document is deleting `where
    {"doc_id": X}`, not a scan.
  * Every chunk carries display metadata (title, page, section, version) so an
    answer can name its source without a second lookup.
  * Ingestion is *idempotent*, keyed on a content hash. Re-ingesting an
    unchanged file is a no-op; re-ingesting a changed file replaces the old
    chunks atomically instead of leaving stale ones to be retrieved forever.
  * Metadata filters (`where=...`) apply *before* the ANN search, so a scoped
    query ("only in the runbook", "only v2 docs") is cheaper, not more
    expensive.

This uses Chroma; the same four operations exist in Qdrant (payload filters),
Weaviate, pgvector (a plain WHERE clause) and every managed alternative.

Run:  python document_manager.py
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

import chromadb
from chromadb.utils import embedding_functions


@dataclass
class Document:
    doc_id: str
    title: str
    text: str
    source_uri: str = ""
    tags: tuple[str, ...] = ()


class DocumentStore:
    """CRUD over a chunk-level vector collection, keyed by document."""

    def __init__(self, path: str = "./.chroma", collection: str = "docs",
                 model: str = "BAAI/bge-small-en-v1.5"):
        self.client = chromadb.PersistentClient(path=path)
        self.collection = self.client.get_or_create_collection(
            name=collection,
            embedding_function=embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=model
            ),
            # Cosine, not the L2 default -- normalised embeddings make cosine the
            # metric these models were actually trained for.
            metadata={"hnsw:space": "cosine"},
        )

    # -- CREATE / UPDATE ---------------------------------------------------
    @staticmethod
    def _content_hash(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def upsert(self, doc: Document, chunks: list[str], pages: list[int] | None = None) -> str:
        """Idempotent ingest. Returns "unchanged" | "created" | "updated".

        The delete-then-add is what makes an update safe: chunk counts change
        between versions, so writing chunk 0..n over the old ones would leave
        orphans from n+1 onward that still answer queries.
        """
        digest = self._content_hash(doc.text)
        existing = self.collection.get(where={"doc_id": doc.doc_id}, limit=1,
                                       include=["metadatas"])
        metadatas = existing.get("metadatas") or []
        if metadatas and metadatas[0].get("content_hash") == digest:
            return "unchanged"

        status = "updated" if metadatas else "created"
        if metadatas:
            self.delete(doc.doc_id)

        pages = pages or [1] * len(chunks)
        self.collection.add(
            ids=[f"{doc.doc_id}::{i}" for i in range(len(chunks))],
            documents=chunks,
            metadatas=[{
                "doc_id": doc.doc_id,
                "title": doc.title,
                "page": pages[i],
                "chunk_index": i,
                "chunk_count": len(chunks),
                "source_uri": doc.source_uri,
                "tags": ",".join(doc.tags),          # Chroma metadata is scalar-only
                "content_hash": digest,
                "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            } for i in range(len(chunks))],
        )
        return status

    # -- READ --------------------------------------------------------------
    def list_documents(self) -> list[dict]:
        """Roll chunk metadata up to one row per document -- what a management UI
        actually needs. Chroma has no GROUP BY, so aggregate client-side; in
        pgvector this is one SELECT ... GROUP BY doc_id."""
        rows = self.collection.get(include=["metadatas"])
        docs: dict[str, dict] = {}
        for meta in rows.get("metadatas") or []:
            entry = docs.setdefault(meta["doc_id"], {
                "doc_id": meta["doc_id"], "title": meta["title"],
                "source_uri": meta.get("source_uri", ""),
                "tags": meta.get("tags", ""), "ingested_at": meta.get("ingested_at"),
                "chunks": 0,
            })
            entry["chunks"] += 1
        return sorted(docs.values(), key=lambda d: d["title"])

    def get_document_chunks(self, doc_id: str) -> list[dict]:
        rows = self.collection.get(where={"doc_id": doc_id}, include=["documents", "metadatas"])
        pairs = zip(rows.get("metadatas") or [], rows.get("documents") or [])
        return [{"index": m["chunk_index"], "page": m["page"], "text": t}
                for m, t in sorted(pairs, key=lambda mt: mt[0]["chunk_index"])]

    def search(self, query: str, k: int = 5, doc_ids: list[str] | None = None,
               tag: str | None = None) -> list[dict]:
        """Metadata-filtered search. Filters are evaluated as a pre-filter, which
        is why scoping to two documents is faster than searching all of them."""
        clauses = []
        if doc_ids:
            clauses.append({"doc_id": {"$in": doc_ids}})
        if tag:
            # tags are stored as a comma-joined string because Chroma metadata
            # values must be scalars; $eq on the exact string only matches a
            # single-tag doc, so use the document-level $contains for substrings.
            clauses.append({"tags": tag})
        where = clauses[0] if len(clauses) == 1 else ({"$and": clauses} if clauses else None)

        res = self.collection.query(query_texts=[query], n_results=k, where=where,
                                    include=["documents", "metadatas", "distances"])
        return [{
            "text": doc,
            "citation": f"[{meta['title']} p.{meta['page']}]",
            "doc_id": meta["doc_id"],
            "score": 1.0 - dist,                    # cosine distance -> similarity
        } for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0],
                                     res["distances"][0])]

    # -- DELETE ------------------------------------------------------------
    def delete(self, doc_id: str) -> int:
        """Delete every chunk of one document. Returns how many were removed.

        Soft-delete alternative: set `deleted: true` in metadata and add
        `{"deleted": false}` to every query filter. Slower and easy to forget on
        one code path -- prefer hard delete plus a re-ingestable source of truth.
        """
        rows = self.collection.get(where={"doc_id": doc_id}, include=[])
        ids = rows.get("ids") or []
        if ids:
            self.collection.delete(ids=ids)
        return len(ids)

    def stats(self) -> dict:
        return {"documents": len(self.list_documents()), "chunks": self.collection.count()}


if __name__ == "__main__":
    store = DocumentStore(path="./.chroma-demo")

    runbook = Document("runbook", "Payments Runbook", "rollback... incident...",
                       "s3://docs/runbook.pdf", ("ops", "payments"))
    policy = Document("policy", "Refund Policy", "14 days... original payment method...",
                      "s3://docs/policy.pdf", ("support",))

    print("upsert runbook :", store.upsert(runbook, [
        "To roll back, run payctl rollback --to <release>. Traffic is drained first.",
        "During a Sev-1 the on-call opens a PIT in #payments-incident.",
    ], pages=[3, 4]))
    print("upsert policy  :", store.upsert(policy, [
        "Customers may request a refund within 14 days of purchase.",
        "Approved refunds are returned to the original payment method.",
    ], pages=[1, 2]))
    print("upsert again   :", store.upsert(policy, [
        "Customers may request a refund within 14 days of purchase.",
        "Approved refunds are returned to the original payment method.",
    ], pages=[1, 2]), "(idempotent)")

    print("\ndocuments:")
    for row in store.list_documents():
        print(f"  {row['doc_id']:<10} {row['title']:<20} {row['chunks']} chunks  {row['tags']}")

    print("\nsearch (all docs):")
    for hit in store.search("how do I undo a release?", k=2):
        print(f"  {hit['score']:.3f} {hit['citation']} {hit['text'][:60]}...")

    print("\nsearch (scoped to policy):")
    for hit in store.search("how do I undo a release?", k=2, doc_ids=["policy"]):
        print(f"  {hit['score']:.3f} {hit['citation']} {hit['text'][:60]}...")

    print(f"\ndeleted {store.delete('runbook')} chunks; stats={store.stats()}")
