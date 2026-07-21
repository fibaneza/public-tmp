# RAG & Knowledge Bases

> **Scope:** Building retrieval-augmented generation on Amazon Bedrock — Knowledge Bases
> (stores, embeddings, chunking, sync, retrieval APIs, filtering) and RAG technique
> (decomposition, parallelism, hybrid search, reranking, contextual retrieval, GraphRAG,
> evaluation). Current as of July 2026. Document/metadata mechanics live in
> [`../aws_kb.md`](../aws_kb.md) — this file does not repeat them.

---

## Part 1 — Knowledge Bases

### Concept

A Knowledge Base is a **managed RAG pipeline**: point a data-source connector at your content,
Bedrock chunks and embeds it, and writes vectors to a store. You then query with **`retrieve`**
(chunks only) or **`retrieve_and_generate`** (chunks + grounded answer + citations).

### Vector stores — pick by constraint

| Store | Pick when | Notes |
|---|---|---|
| **S3 Vectors** | Moderate scale, infrequent queries, cost-sensitive | ≈90% cheaper; serverless; some filter operators unsupported. |
| **OpenSearch Serverless** | Lowest latency, need **HYBRID** search | Bedrock can "quick create" it; use the `faiss` engine for filtering. |
| **Aurora PostgreSQL (pgvector)** | You already run Aurora | Fewer moving parts, but couples KB load to your DB — isolate it. |
| **Neptune Analytics** | Multi-hop / relationship questions | Powers managed **GraphRAG** (below). |
| Pinecone / Redis / MongoDB Atlas | Existing investment | Bring-your-own; you manage the store. |

### Embedding model

Default to **Amazon Titan Text Embeddings V2** (`amazon.titan-embed-text-v2:0`): 8,192-token
input, output dims **256 / 512 / 1024**, 100+ languages. The dimension tradeoff is a real cost
lever — 512 dims keeps ~99% of 1024-dim accuracy (~50% storage), 256 dims ~97% (~75% storage).
Use **Cohere Embed** for strong multilingual. **Embedding dimensions must match the index** or
ingestion fails, and you cannot change the embedding model without re-ingesting.

### Chunking (immutable after data-source creation)

| Strategy | Best for |
|---|---|
| `FIXED_SIZE` (default 300 tokens, 20% overlap) | Baseline, uniform prose |
| `SEMANTIC` | Legal/technical prose without clear boundaries (extra cost) |
| `HIERARCHICAL` (parent/child) | Nested/structured docs — retrieves child, substitutes parent for context |
| `NONE` | You pre-split the files |
| **Custom** (Lambda) | Contextual retrieval, sliding windows |

Lock chunking in **deliberately before the first sync** — changing it means deleting and
recreating the data source. Note under `HIERARCHICAL` the returned result count can be *fewer*
than `numberOfResults` because children collapse into parents.

### Create a KB + ingest (Python)

```python
import boto3
ba = boto3.client("bedrock-agent")

kb = ba.create_knowledge_base(
    name="docs-kb", roleArn=KB_ROLE_ARN,
    knowledgeBaseConfiguration={"type": "VECTOR", "vectorKnowledgeBaseConfiguration": {
        "embeddingModelArn": f"arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0",
        "embeddingModelConfiguration": {"bedrockEmbeddingModelConfiguration": {"dimensions": 1024}}}},
    storageConfiguration={"type": "OPENSEARCH_SERVERLESS",
        "opensearchServerlessConfiguration": {"collectionArn": COLL_ARN,
            "vectorIndexName": "bedrock-idx",
            "fieldMapping": {"vectorField": "vector", "textField": "text",
                             "metadataField": "metadata"}}})
kb_id = kb["knowledgeBase"]["knowledgeBaseId"]

ds = ba.create_data_source(knowledgeBaseId=kb_id, name="s3",
    dataSourceConfiguration={"type": "S3", "s3Configuration": {"bucketArn": "arn:aws:s3:::my-docs"}},
    vectorIngestionConfiguration={"chunkingConfiguration": {
        "chunkingStrategy": "HIERARCHICAL",
        "hierarchicalChunkingConfiguration": {
            "levelConfigurations": [{"maxTokens": 1500}, {"maxTokens": 300}],
            "overlapTokens": 60}}})

ba.start_ingestion_job(knowledgeBaseId=kb_id,
                       dataSourceId=ds["dataSource"]["dataSourceId"])
```

### Connectors & incremental sync

Connectors: **S3, Web Crawler, SharePoint, Confluence, Salesforce**, plus structured stores
(Redshift/Glue) via `GenerateQuery`. Every connector supports **incremental sync** — after the
first crawl, only new/modified/deleted content is processed. Automate it: an EventBridge rule
on S3 changes triggers `start_ingestion_job`. Alarm on ingestion-job failures.

### Retrieval APIs — `retrieve` vs `retrieve_and_generate`

They are **not** interchangeable — pick deliberately (this is the exact tradeoff the repo's
[`../Plan/kb.py`](../Plan/kb.py) and [`../Plan/README.md`](../Plan/README.md) implement):

| | `retrieve` | `retrieve_and_generate` |
|---|---|---|
| Who generates | **your** model | the model **inside Bedrock** |
| Citations | you assemble | built-in |
| Best when | you want tone/format control, multi-tool reasoning over chunks | turnkey cited doc Q&A with minimal glue |

**The double-generation caveat:** exposing `retrieve_and_generate` as an agent tool means the
agent's model calls a tool that *already generated* an answer, then may re-generate on top.
Mitigate by keeping the tool single-shot and instructing the agent to treat the KB answer as
authoritative. If the agent orchestrates many tools per turn, prefer `retrieve` and let the
agent generate once.

### Metadata filtering & multi-tenancy

Attach a `.metadata.json` sidecar per object (mechanics in [`../aws_kb.md`](../aws_kb.md)), then
filter at query time — operators `equals`, `in`, `greaterThan`, `startsWith`, `stringContains`,
etc., combined with `andAll`/`orAll`. This is the primary mechanism for **multi-tenant RAG**:
one KB, filter by `tenant_id`.

```python
rt = boto3.client("bedrock-agent-runtime")
rt.retrieve(knowledgeBaseId=kb_id, retrievalQuery={"text": q},
    retrievalConfiguration={"vectorSearchConfiguration": {
        "numberOfResults": 10,
        "filter": {"equals": {"key": "tenant_id", "value": tenant_id}}}})
```

**Security note:** any principal with `bedrock:Retrieve` sees *all* synced data — tenant
isolation is enforced by the filter you apply, so make it non-optional in your code path.
Caveats: OpenSearch Serverless needs the `faiss` engine for filtering; managed KBs and S3
Vectors don't support `startsWith`/`stringContains`.

---

## Part 2 — RAG techniques

### Query decomposition (built-in)

A compound question blended into one embedding suffers "semantic dilution." Decomposition
splits it into sub-queries, retrieves each **separately**, pools and ranks, then generates.
Turn it on with one config flag:

```python
rt.retrieve_and_generate(
    input={"text": "Where is Octank's waterfront HQ, and how did the scandal hurt its image?"},
    retrieveAndGenerateConfiguration={"type": "KNOWLEDGE_BASE",
        "knowledgeBaseConfiguration": {"knowledgeBaseId": kb_id, "modelArn": MODEL_ARN,
            "orchestrationConfiguration": {
                "queryTransformationConfiguration": {"type": "QUERY_DECOMPOSITION"}}}})
```

### Parallel multi-query fan-out (build-it-yourself)

For maximum control, decompose with a Converse call, then run `retrieve` **concurrently** and
merge. The one gotcha that causes intermittent bugs: **boto3 clients are not thread-safe** —
create a session and client **per thread**.

```python
from concurrent.futures import ThreadPoolExecutor
import boto3

def retrieve_one(q):
    c = boto3.session.Session().client("bedrock-agent-runtime")   # per-thread client
    return c.retrieve(knowledgeBaseId=kb_id,
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 10}},
        retrievalQuery={"text": q})["retrievalResults"]

with ThreadPoolExecutor(max_workers=6) as ex:
    pooled = [r for sub in ex.map(retrieve_one, sub_queries) for r in sub]
# dedup by chunk location, then rerank the union (below)
```

This "multi-query fan-out" also generalizes to fanning out across **multiple KBs** (per-domain
stores) and merging — the pattern is the same: parallelize independent retrievals, then dedup
and rerank.

### Hybrid search

`overrideSearchType: "HYBRID"` (vector + keyword) improves recall on exact terms, IDs, and
acronyms. Only valid on OpenSearch Serverless / Aurora / MongoDB with a filterable text field —
elsewhere it silently falls back to semantic. The repo's `kb_search` tool leaves it configurable
via `KB_SEARCH_TYPE` for exactly this reason.

### Reranking (two-stage retrieval)

Over-retrieve, then cross-encoder **rerank** to precision. The Bedrock **Rerank API**
(`bedrock-agent-runtime.rerank`) offers **Cohere Rerank 3.5** (`cohere.rerank-v3-5:0`) and
**Amazon Rerank**; it can rerank raw docs without a KB, or plug into
`retrieve`/`retrieve_and_generate` via `rerankingConfiguration`.

```python
rt.rerank(
    queries=[{"type": "TEXT", "textQuery": {"text": q}}],
    sources=[{"type": "INLINE", "inlineDocumentSource":
              {"type": "TEXT", "textDocument": {"text": d}}} for d in docs],
    rerankingConfiguration={"type": "BEDROCK_RERANKING_MODEL",
        "bedrockRerankingConfiguration": {"numberOfResults": 5,
            "modelConfiguration": {
                "modelArn": f"arn:aws:bedrock:{region}::foundation-model/cohere.rerank-v3-5:0"}}})
```

### Contextual retrieval

Prepend an LLM-generated context blurb to each chunk **before embedding** (Anthropic's
technique) so a chunk carries enough surrounding context to be found. Implement with a **custom
chunking Lambda** on the KB. Reduces "lost context" from naive splitting.

### GraphRAG

Managed GraphRAG (Neptune Analytics) auto-extracts entities/relationships and traverses the
graph for **multi-hop, explainable** answers in one API call — no graph expertise required.
Reach for it when questions chain across related entities. For structured databases,
`GenerateQuery` does NL→SQL.

### Citations

`retrieve_and_generate` returns `citations[]` with `generatedResponsePart` and
`retrievedReferences` (S3 URI, and `x-amz-bedrock-kb-document-page-number` for PDFs). **Always
surface them** — the repo's `RagResult.formatted()` appends a de-duplicated source list.

### Evaluation — don't tune blind

Use **Bedrock RAG evaluation** (LLM-as-a-judge, GA) to score retrieval and generation:
context relevance/coverage, **citation precision & coverage**, correctness, completeness,
faithfulness, plus responsible-AI metrics (0–1 with published rubrics). Provide a JSONL prompt
dataset in S3 (optionally with ground truth). Measure **before and after** every chunking,
reranking, or model change. Open-source **RAGAS** is a fine CI alternative.

## Do

- Decompose complex/multi-part queries; over-retrieve then rerank down.
- Fan out sub-queries (or multiple KBs) concurrently with **per-thread** boto3 clients.
- Use hybrid search where the store supports it; enable contextual grounding on generation.
- Automate incremental sync via EventBridge; alarm on ingestion failures.
- Gate every RAG change behind an evaluation run measuring citation precision/coverage.

## Don't

- Don't blend a compound question into one embedding — decompose it.
- Don't share one boto3 client across threads for parallel retrieval.
- Don't change chunking or embedding model after ingestion without re-ingesting.
- Don't assume `HYBRID` is active on an unsupported store, or that `numberOfResults` equals the
  returned count under hierarchical chunking.
- Don't rely on the retrieval filter being applied "somewhere else" for tenant isolation — make
  it non-optional in your code.

## Sources

- [Retrieval how-it-works](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-how-retrieval.html) · [test/query config (decomposition, hybrid, filters)](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-test-config.html)
- [Chunking](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-chunking.html) · [Titan Text Embeddings V2](https://docs.aws.amazon.com/bedrock/latest/userguide/titan-embedding-models.html)
- [Advanced parsing, chunking & query reformulation](https://aws.amazon.com/blogs/machine-learning/amazon-bedrock-knowledge-bases-now-supports-advanced-parsing-chunking-and-query-reformulation-giving-greater-control-of-accuracy-in-rag-based-applications/)
- [Rerank API](https://docs.aws.amazon.com/bedrock/latest/userguide/rerank-use.html) · [Cohere Rerank 3.5 on Bedrock](https://aws.amazon.com/blogs/machine-learning/cohere-rerank-3-5-is-now-available-in-amazon-bedrock-through-rerank-api/)
- [Contextual retrieval on Bedrock](https://aws.amazon.com/blogs/machine-learning/contextual-retrieval-in-anthropic-using-amazon-bedrock-knowledge-bases/)
- [GraphRAG with Neptune Analytics (GA)](https://aws.amazon.com/blogs/machine-learning/announcing-general-availability-of-amazon-bedrock-knowledge-bases-graphrag-with-amazon-neptune-analytics/)
- [Multi-tenancy with metadata filtering](https://aws.amazon.com/blogs/machine-learning/multi-tenancy-in-rag-applications-in-a-single-amazon-bedrock-knowledge-base-with-metadata-filtering/)
- [RAG evaluation & LLM-as-a-judge (GA)](https://aws.amazon.com/blogs/aws/new-rag-evaluation-and-llm-as-a-judge-capabilities-in-amazon-bedrock/) · [evaluation docs](https://docs.aws.amazon.com/bedrock/latest/userguide/evaluation.html)
- Repo: [`../aws_kb.md`](../aws_kb.md) · [`../Plan/kb.py`](../Plan/kb.py) · [`../Plan/README.md`](../Plan/README.md)
