# Performance — Recommendations & Techniques

> **Scope:** Making Bedrock + AgentCore workloads fast and scalable — latency, throughput,
> concurrency, and efficiency. Distinct from cost ([`01`](01-architecture-and-design.md)) though
> the levers overlap. Measure first (see [`10`](10-observability-tracing-logs-monitoring.md)),
> then optimize the axis that's actually failing your SLO. Current as of July 2026.

## The three axes (optimize the one that's failing)

| Axis | Metric | Primary levers |
|---|---|---|
| **Latency** | time-to-first-token, p50/p90/p99 end-to-end | model tier, prompt caching, streaming, token reduction, latency-optimized inference |
| **Throughput** | requests/sec, tokens/sec | CRIS, Provisioned Throughput, concurrency, connection pooling, batch |
| **Efficiency** | tokens (and CPU-seconds) per unit of work | context pruning, caching, right-sized retrieval, slim images |

Don't optimize blind — instrument time-to-first-token, tail latency, throttle rate, and cache
hit rate before changing anything.

## 1. Latency — model & inference

- **Tier the model.** Route cheap/fast intents (classification, routing, short answers) to
  Haiku / Nova Micro/Lite; reserve Sonnet/Opus for hard reasoning. Output tokens dominate
  latency, so a smaller model on a short task wins twice.
- **Prompt caching** is the biggest single latency win (~85% on cache hits) for stable prefixes
  — system prompt, tool definitions, long RAG context. Place a `cachePoint` after the stable
  block; ~5-min sliding TTL.

```python
resp = boto3.client("bedrock-runtime").converse(
    modelId=MODEL_ID,
    system=[{"text": LONG_SYSTEM_PROMPT}, {"cachePoint": {"type": "default"}}],
    messages=[{"role": "user", "content": [{"text": user_msg}]}],
)
# watch resp["usage"]["cacheReadInputTokenCount"] / cacheWriteInputTokenCount
```

- **Stream** to cut *perceived* latency — `converse_stream` (or Runtime SSE, see
  [`02`](02-agentcore-runtime.md)) gets tokens to the user immediately. Time-to-first-token, not
  total time, is what users feel.
- **Latency-optimized inference** where the model/Region supports it:
  `performanceConfig={"latency": "optimized"}` on the Converse call.
- **Cut input tokens** — trim the system prompt, prune retrieved chunks to the top few after
  reranking, and compact history via Memory summaries rather than replaying every turn.
- **Cut output tokens** — set a tight `maxTokens`, ask for concise or structured output, and
  stop sequences; don't let the model ramble.
- **Provisioned Throughput** buys consistent, predictable latency for steady high volume (no
  noisy-neighbor variance).

## 2. Throughput & concurrency

- **Cross-Region Inference (CRIS)** — geographic/global inference profiles raise aggregate
  throughput and absorb bursts (fewer `503`/throttles). Just remember the two-ARN IAM policy
  ([`08`](08-security-roles-and-permissions.md)).
- **Parallelize independent work** — parallel tool calls, parallel RAG retrieval / sub-query
  fan-out (see the fan-out pattern in [`06`](06-rag-and-knowledge-bases.md)), and concurrent
  downstream calls. On AgentCore Runtime, billing is on *active CPU*, so overlapping LLM/tool
  I/O waits is effectively free — structure for concurrency.
- **Reuse boto3 clients and size the connection pool.** Creating a client per call is slow;
  under-sized pools serialize concurrent calls. Reuse one client per thread and raise
  `max_pool_connections`. Add adaptive retries so throttles back off instead of hammering.

```python
from botocore.config import Config
cfg = Config(max_pool_connections=50, retries={"max_attempts": 5, "mode": "adaptive"})
rt = boto3.client("bedrock-runtime", config=cfg)   # reuse this; one Session/client per thread
```

- **Batch inference** for offline/bulk work — higher throughput per dollar, not for
  interactive latency.
- **Handle backpressure** — treat `ThrottlingException` as a capacity signal: exponential
  backoff with jitter (adaptive mode does this), shed or queue load, and scale via CRIS /
  Provisioned Throughput rather than tighter retries.

## 3. AgentCore Runtime performance

- **Shrink cold starts.** Slim ARM64 image (`uv`, `--no-dev`), lazy-import heavy libraries, and
  keep import-time work minimal — the repo's [`../Plan/agent.py`](../Plan/agent.py) lazy-imports
  `strands_tools` so the agent starts even when RAG is off and pays the import cost only when
  used.
- **Reuse the session.** Reusing a `runtimeSessionId` within the 15-minute idle window keeps a
  warm microVM and avoids re-initialization ([`05`](05-session-and-memory.md)).
- **Offload long work** to the async-task pattern so you're not holding a synchronous
  connection for minutes/hours.
- **Keep durable state in Memory,** not recomputed per invocation; don't rebuild expensive
  context every turn.
- **Right-size the payload** — inline small multimodal data (base64) but stage large artifacts
  in S3; the 100 MB ceiling is not a fast data channel.

## 4. RAG / retrieval performance

- **Vector store by latency profile** — OpenSearch Serverless for lowest, most consistent query
  latency; S3 Vectors is cheaper but has higher/variable latency for infrequent queries.
- **Smaller embedding dimensions** (Titan v2 at 512 or 256) shrink vectors → faster search and
  less storage, with minor recall loss.
- **Narrow the search** — metadata filters and namespace scoping cut the candidate set, which is
  both faster and more relevant.
- **Budget the two-stage cost** — over-retrieve → rerank improves quality but adds a hop; cap
  `numberOfResults` and rerank only the union. Add reranking only where relevance actually
  demands it.
- **Cache hot retrievals** at the app layer for popular/repeated queries.
- **Keep ingestion off the hot path** — incremental sync on a schedule/event, never at query
  time ([`06`](06-rag-and-knowledge-bases.md)).

## 5. Memory performance

- **Bound short-term loads** — `get_last_k_turns` with a small `k`; don't replay unbounded
  history into every prompt.
- **Tighten long-term retrieval** — higher `relevance_score` and small `top_k` per namespace
  keep the prompt lean (faster + cheaper); the repo's [`../Plan/memory.py`](../Plan/memory.py)
  tunes preferences tight and facts broad.
- **Never block on LTM consolidation** — it's asynchronous; use STM for the immediate turn.

## 6. Guardrails performance

- **Sequential vs parallel `ApplyGuardrail`** — running the input check in parallel with
  inference cuts latency but pays for both even on a block; sequential skips inference on a block
  ([`07`](07-guardrails-and-pii.md)). Choose per risk profile.
- **Streaming mode** — synchronous scanning is safer but adds latency; asynchronous emits
  immediately (but can't mask PII).
- **Scope policies** to what each stage needs — every `ApplyGuardrail` call is a network hop.

## 7. Measure & set targets

- Track **time-to-first-token**, end-to-end **p50/p90/p99**, **tokens/sec**, **throttle rate**,
  and **cache hit rate** — emit them via OTel GenAI attributes ([`10`](10-observability-tracing-logs-monitoring.md)).
- **Load-test before launch** at expected peak concurrency; find the throttle ceiling and size
  CRIS / Provisioned Throughput accordingly.
- **Set SLOs** and alarm on tail latency and throttles, not just averages — p99 is where users
  churn.

## Do

- Instrument first; optimize the axis that's failing the SLO.
- Cache stable prefixes; stream responses; tier models by task.
- Reuse clients, size the connection pool, and use adaptive retries.
- Parallelize independent retrieval/tool calls; overlap I/O on Runtime.
- Prune input and cap output tokens; keep retrieval narrow and reranking selective.
- Load-test to the throttle ceiling and size CRIS / Provisioned Throughput to it.

## Don't

- Don't optimize on averages — chase p90/p99 and time-to-first-token.
- Don't create a boto3 client per call or share one client across threads.
- Don't answer fast intents with a large model, or replay unbounded history into the prompt.
- Don't treat retries as a capacity fix — throttles mean scale out (CRIS/PT), not retry harder.
- Don't put ingestion, provisioning, or unbounded memory loads on the request path.

## Sources

- [Prompt caching](https://aws.amazon.com/bedrock/prompt-caching/) · [effective prompt caching](https://aws.amazon.com/blogs/machine-learning/effectively-use-prompt-caching-on-amazon-bedrock/)
- [Latency-optimized inference](https://docs.aws.amazon.com/bedrock/latest/userguide/latency-optimized-inference.html)
- [Cross-Region inference](https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html) · [Provisioned Throughput](https://docs.aws.amazon.com/bedrock/latest/userguide/prov-throughput.html)
- [botocore retries (adaptive mode)](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/retries.html) · [botocore Config](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/core/session.html)
- [Runtime — how it works (active-CPU billing, sessions)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html)
- [Well-Architected Generative AI Lens — performance](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/generative-ai-lens.html) · [Agentic AI Lens `AGENTPERF01-BP02`](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentperf01-bp02.html)
- Related: [`01-architecture-and-design.md`](01-architecture-and-design.md) (cost), [`06`](06-rag-and-knowledge-bases.md) (retrieval), [`10`](10-observability-tracing-logs-monitoring.md) (metrics)
