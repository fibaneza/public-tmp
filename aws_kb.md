# AWS Bedrock Knowledge Bases — Metadata & File Structure Best Practices

Reference for structuring source documents (PDF, Markdown, HTML) and their sidecar
metadata for Amazon Bedrock Knowledge Bases (S3 data source), current as of July 2026.

## 1. Supported formats and hard limits

| Format | Extension | Notes |
|---|---|---|
| Plain text | `.txt` | UTF-8 |
| Markdown | `.md` | UTF-8 |
| HTML | `.html` | UTF-8 |
| Word | `.doc` / `.docx` | |
| CSV | `.csv` | RFC4180, UTF-8, header row required |
| Excel | `.xls` / `.xlsx` | |
| PDF | `.pdf` | Text, or multimodal via BDA/foundation-model parser |
| Images | `.jpeg` / `.png` | Multimodal only, max 3.75 MB |

- Max file size: **50 MB** per source document.
- Metadata sidecar file: max **10 KB**.
- Foundation-model parser: total data source size ≤ **100 GB**.

## 2. S3 object layout

Keep the metadata file next to its source object, same base name, `.metadata.json` suffix:

```
s3://my-kb-bucket/
└── documents/
    ├── policies/
    │   ├── security-policy-2026.pdf
    │   ├── security-policy-2026.pdf.metadata.json
    │   ├── onboarding-guide.md
    │   └── onboarding-guide.md.metadata.json
    └── api-docs/
        ├── auth-endpoints.html
        └── auth-endpoints.html.metadata.json
```

Practical rules:

- Use an `inclusionPrefixes` (e.g. `documents/`) on the data source instead of pointing at
  bucket root — makes incremental sync and IAM scoping cleaner.
- One metadata file per source file. It **must** live in the same S3 "folder" as the source.
- Don't put spaces or special characters in keys/filenames — some connectors and downstream
  tooling (Lambda transforms, CI pipelines) choke on them.
- Version documents by filename or metadata attribute (`version: "2026-07"`), not by
  overwriting in place — overwrites trigger re-embedding of the whole file on next sync, and
  you lose the ability to filter old vs. new content during a rollout.

## 3. Metadata file format

Full schema (gives per-attribute control, including whether the value is folded into the
embedding):

```json
{
  "metadataAttributes": {
    "department": {
      "value": { "type": "STRING", "stringValue": "Security" },
      "includeForEmbedding": true
    },
    "doc_type": {
      "value": { "type": "STRING", "stringValue": "policy" },
      "includeForEmbedding": false
    },
    "created_date": {
      "value": { "type": "NUMBER", "numberValue": 20260701 },
      "includeForEmbedding": false
    },
    "is_current": {
      "value": { "type": "BOOLEAN", "booleanValue": true },
      "includeForEmbedding": false
    },
    "tags": {
      "value": { "type": "STRING_LIST", "stringListValue": ["iso27001", "internal"] },
      "includeForEmbedding": false
    }
  }
}
```

Simplified form (no embedding control — always stored for filtering only, never embedded):

```json
{ "metadataAttributes": { "department": "Security", "doc_type": "policy" } }
```

**`includeForEmbedding: true`** prepends `key: value` to the chunk text before it's embedded, so
a query containing that word/value boosts similarity score for that chunk. Use this sparingly —
only for attributes a user might phrase *in natural language* (e.g. "the security policy"). Use
`false` (or the simplified form) for attributes that exist purely to be filtered on
programmatically (dates, IDs, booleans) — folding a raw epoch timestamp into the embedding adds
noise, not signal.

Supported data types: `STRING`, `NUMBER`, `BOOLEAN`, `STRING_LIST`.

### CSV-specific variant

CSV data sources use a different (record-based) shape, since one file maps to many chunks
(one per row):

```json
{
  "metadataAttributes": { "source": "property_listings_2026" },
  "documentStructureConfiguration": {
    "type": "RECORD_BASED_STRUCTURE_METADATA",
    "recordBasedStructureMetadata": {
      "contentFields": [{ "fieldName": "description" }],
      "metadataFieldsSpecification": {
        "fieldsToInclude": [{ "fieldName": "city" }, { "fieldName": "price" }]
      }
    }
  }
}
```

Only one `contentField` is supported per CSV; every other included column becomes a
per-chunk string metadata attribute. Not applicable to PDF/MD/HTML.

## 4. Reserved fields — do not collide

- Custom (self-managed vector store) knowledge bases reserve the `x-amz-bedrock` prefix.
- Fully-managed knowledge bases reserve underscore-prefixed fields, e.g. `_source_uri`,
  `_data_source_id`. Bedrock also auto-populates `x-amz-bedrock-kb-document-page-number` for
  PDFs when chunking (not available if you choose "no chunking").
- You cannot override these — pick your own attribute names that don't start with these
  prefixes (`doc_type` not `_doc_type`, etc.).

## 5. Filtering-oriented naming conventions

Design metadata around how you'll *query* it later, not how the document happens to be
organized in a file share:

- Use flat, filterable keys: `department`, `region`, `doc_type`, `sensitivity`, `product_line`,
  `is_current`, `effective_date` (as `NUMBER` epoch or `YYYYMMDD` int, not a free-text date
  string — filters are exact-match/range, not date-parsing).
- Prefer `BOOLEAN`/enum-like `STRING` over free text for anything you'll filter on
  (`sensitivity: "internal" | "confidential" | "public"`), so filters stay simple equality
  checks instead of fuzzy string matching (`startsWith`/`stringContains` aren't supported on
  managed knowledge bases).
- Use `STRING_LIST` for multi-valued facets (`tags`, `applies_to_teams`) instead of a
  delimited string you'd have to parse client-side.
- Keep a stable attribute schema across the whole data source — mixed types for the same key
  across files (e.g. `created_date` as string in some files, number in others) breaks filter
  queries that assume one type.

## 6. Format-specific guidance

### PDF
- Default parser extracts text only. If PDFs contain tables, charts, or scanned/image content,
  set the data source parser to **Bedrock Data Automation** or a **foundation model** parser —
  the default parser silently drops that content. Note: whichever parser you choose applies to
  *every* PDF in the data source, even pure-text ones, and both are billed (per-page or
  per-token respectively).
- Bedrock auto-tracks page numbers for citations (`x-amz-bedrock-kb-document-page-number`) —
  only available if you use a real chunking strategy, not "no chunking."
- For long PDFs (specs, contracts) hierarchical chunking (parent/child) tends to preserve
  context better than fixed-size chunking, since retrieval swaps a small matched chunk for its
  full parent section.

### Markdown
- Cheapest to parse well — default parser handles `.md` natively as text, structure (headers)
  is preserved in the token stream, so default/semantic chunking respects section boundaries
  reasonably.
- Good candidate for metadata like `doc_type: "runbook"` / `doc_type: "adr"` since Markdown is
  usually already organized by convention (READMEs, ADRs, runbooks) — mirror that convention
  into a filterable attribute rather than relying on users' knowledge base to be able to search
  path structure directly (S3 path is not automatically filterable metadata unless you add it
  yourself, e.g. `source_path`).

### HTML
- Default parser strips markup and keeps text; for parsed/converted content Bedrock chunks
  respecting logical document boundaries and won't merge across them, even if that leaves a
  chunk smaller than your configured max size.
- Strip boilerplate (nav bars, footers, cookie banners) before ingestion if possible — the
  parser doesn't know what's "chrome" vs. content, so navigation text ends up embedded and can
  pollute retrieval relevance.

## 7. Practical checklist

- [ ] One `<file>.<ext>.metadata.json` per source file, same S3 prefix, ≤ 10 KB.
- [ ] Attribute names avoid `x-amz-bedrock*` and leading-underscore reserved prefixes.
- [ ] Consistent type per attribute key across the whole data source.
- [ ] `includeForEmbedding: true` used only for attributes worth boosting semantic recall;
      everything else `false`.
- [ ] Dates as `NUMBER` (epoch or `YYYYMMDD`), not free-text strings, if you'll filter/sort on
      them.
- [ ] Multi-valued facets as `STRING_LIST`, not delimited strings.
- [ ] PDFs with tables/images routed through BDA or a foundation-model parser; pure-text PDFs
      left on the default parser to avoid unnecessary parsing cost.
- [ ] Chunking strategy locked in deliberately before first sync — **it can't be changed after
      the data source is created** without deleting and recreating it.
- [ ] `inclusionPrefixes` scoped to a subfolder, not the bucket root.

## Sources

- [Include metadata in a data source to improve knowledge base query](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-metadata.html)
- [Connect to Amazon S3 for your knowledge base](https://docs.aws.amazon.com/bedrock/latest/userguide/s3-data-source-connector.html)
- [Prerequisites for your Amazon Bedrock knowledge base data](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-ds.html)
- [How content chunking works for knowledge bases](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-chunking.html)
- [Parsing options for your data source](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-advanced-parsing.html)
- [Amazon Bedrock Knowledge Bases now supports metadata filtering to improve retrieval accuracy](https://aws.amazon.com/blogs/machine-learning/amazon-bedrock-knowledge-bases-now-supports-metadata-filtering-to-improve-retrieval-accuracy/)
