# AgentCore + Strands Agent — Session, Memory, Knowledge Base

A minimal, production-shaped Strands agent for **Amazon Bedrock AgentCore
Runtime** that demonstrates the three pieces from the architecture doc:

- **Sessions** — conversation isolation via the runtime-supplied `session_id`.
- **Memory** — short-term (turn history) + long-term (preferences, facts,
  summaries) via the managed **AgentCore Memory** service.
- **Knowledge Base** — RAG over a **Bedrock Knowledge Base**, with a choice of
  two strategies (see "RAG modes" below).

No LangGraph — Strands is AWS's reference SDK for AgentCore, so this wiring
is first-class rather than glued on.

## RAG modes

The KB supports two strategies, selected by `KB_MODE`. They are **not**
interchangeable — pick deliberately:

| | `retrieve` | `retrieve_and_generate` (default) |
| --- | --- | --- |
| API | native Bedrock `Retrieve` | native Bedrock `RetrieveAndGenerate` |
| Who generates the answer | **your agent's** model | the model **inside Bedrock** |
| Citations | you assemble them | built-in |
| Code / cost | more code, one generation | less code, built-in grounding |
| Best when | you want full control of tone, format, and multi-tool reasoning over chunks | you want consistent, cited doc Q&A with minimal glue |

**The double-generation caveat.** When `retrieve_and_generate` is exposed as
an agent tool, the agent's model calls a tool that *already generated* an
answer — and may then re-generate on top of it. This project mitigates that by
(a) keeping the tool call single-shot and (b) instructing the agent to treat
the KB answer as authoritative and pass it through with its citations. If your
use case is *pure* document Q&A with no tool orchestration, skip the agent
entirely and call the API directly:

```bash
python -m scripts.kb_qa "What is our refund policy for enterprise plans?"
```

`RetrieveAndGenerate` also manages its **own** conversational `sessionId`
(Bedrock generates it — you can't set it, only reuse it). That is separate
from AgentCore Memory's session. Inside the agent we deliberately **don't**
thread the KB session, so AgentCore Memory remains the single source of
conversation continuity; the standalone script shows how to thread it when the
KB *is* the whole app.

## Why it's shaped this way

The design mirrors the companion ADR (`agentcore-vs-backend.md`):

- **Deterministic vs. agentic split.** This agent orchestrates and reasons;
  it does **not** own business invariants. Business data and actions go
  through your FastAPI backend, exposed to the agent as tools via AgentCore
  Gateway. The agent never opens a DB connection.
- **Memory is a managed service, not an Aurora schema.** Session and
  long-term memory live in AgentCore Memory and are reached through the SDK —
  the boundary is enforced by service isolation, not discipline.
- **Declarative memory over hooks.** We use `AgentCoreMemorySessionManager`
  so the SDK owns save/load correctness. A hook-based alternative
  (`app/memory_hooks.py`) is included for when you need custom retrieval.
- **Provisioning is out of band.** The Memory resource and Knowledge Base are
  created once (script / IaC / console), then referenced by ID at deploy
  time. Nothing on the request path creates AWS resources.

## Layout

```text
agentcore-strands-agent/
├── app/
│   ├── config.py          # env-driven settings, fail-fast, KB var normalisation
│   ├── memory.py          # AgentCoreMemorySessionManager (declarative — default)
│   ├── memory_hooks.py    # hook-based memory (alternative, for custom control)
│   ├── kb.py              # native Retrieve + RetrieveAndGenerate (kb_search / knowledge_base_qa)
│   ├── session_meta.py    # per-turn session-metadata upsert to Aurora (RDS Data API)
│   ├── agent.py           # agent factory: model + memory + RAG + gateway tools
│   └── runtime.py         # BedrockAgentCoreApp entrypoint (/invocations)
├── scripts/
│   ├── provision_memory.py  # RUN ONCE: create the Memory resource + strategies
│   └── kb_qa.py             # standalone RetrieveAndGenerate (no agent)
├── db/
│   └── schema.sql         # agent_meta schema + role grants (metadata only)
├── docs/
│   ├── architecture-v2.md      # target architecture + design review
│   └── system-overview.mermaid # system diagram
├── requirements.txt
└── README.md
```

## Install

Python 3.10+ recommended.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Configure AWS credentials (`aws configure`) with permissions for Bedrock,
AgentCore, and (for RAG) the Knowledge Base. Request Bedrock **model access**
for your chosen model in the console first.

## Setup (once)

### 1. Provision the Memory resource

```bash
python -m scripts.provision_memory --name AppMemory --region us-west-2
# → prints: BEDROCK_AGENTCORE_MEMORY_ID=mem-xxxxxxxx
```

The strategies it creates (`/preferences/{actorId}`, `/facts/{actorId}`,
`/summaries/{actorId}/{sessionId}`) must match the `retrieval_config`
namespaces in `app/memory.py`. If you change one, change the other.

### 2. Create a Knowledge Base (optional, for RAG)

Create a Bedrock Knowledge Base backed by an S3 data source in the console or
via IaC, sync your documents, and note its ID. RAG is skipped automatically
when no KB ID is set.

## Configure

| Variable | Required | Purpose |
| --- | --- | --- |
| `AWS_REGION` | recommended | AWS region (default `us-west-2`). |
| `BEDROCK_MODEL_ID` | no | Bedrock model / inference-profile ID. |
| `BEDROCK_AGENTCORE_MEMORY_ID` | no* | Enables memory. Unset → stateless. |
| `STRANDS_KNOWLEDGE_BASE_ID` | no* | Enables RAG. `KNOWLEDGE_BASE_ID` also accepted. |
| `KB_MODE` | no | `retrieve_and_generate` (default) or `retrieve`. |
| `KB_MODEL_ARN` | if inference profile | Model ARN for RetrieveAndGenerate. Required for `us./eu./apac.` inference-profile models. |
| `KB_NUM_RESULTS` | no | Chunks retrieved before generation (default 5). |
| `KB_SEARCH_TYPE` | no | `HYBRID` or `SEMANTIC` for the Retrieve API (store-dependent). |
| `AURORA_CLUSTER_ARN` | no* | Enables session-metadata upserts (RDS Data API). |
| `AURORA_SECRET_ARN` | no* | Secrets Manager secret for the Data API. |
| `AURORA_DATABASE` | no* | Target database. All three required together. |
| `DEFAULT_ACTOR_ID` | no | Local-only fallback user id. |

See `docs/architecture-v2.md` for the full target architecture (Gateway-fronted
Runtime, Bedrock KB best practices, session metadata in `agent_meta`) and
`docs/system-overview.mermaid` for the diagram. Apply `db/schema.sql` before
enabling metadata writes.

\* Optional in the sense that the agent still runs without them — it just
loses that capability. Set both for the full experience.

## Run locally

```bash
export AWS_REGION=us-west-2
export BEDROCK_AGENTCORE_MEMORY_ID=mem-xxxxxxxx
export STRANDS_KNOWLEDGE_BASE_ID=kb-xxxxxxxx
python -m app.runtime
```

Invoke the local `/invocations` endpoint:

```bash
curl -s localhost:8080/invocations \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "What is our refund policy?", "actor_id": "user-123"}'
```

Locally you pass `actor_id` in the payload; on Runtime, prefer the
authenticated identity from the request (see `_resolve_actor_id`). `session_id`
is supplied automatically by the Runtime and falls back to a default locally.

## Deploy to AgentCore Runtime

```bash
agentcore launch \
  --env BEDROCK_AGENTCORE_MEMORY_ID=mem-xxxxxxxx \
  --env STRANDS_KNOWLEDGE_BASE_ID=kb-xxxxxxxx \
  --env AWS_REGION=us-west-2
```

## Wiring in your FastAPI backend as tools

Business operations should reach the agent as **AgentCore Gateway** tools, not
as direct DB access. Point a Gateway at your OpenAPI spec or Lambda targets,
then load those tools in `app/agent.py` where `_load_tools()` has the
`gateway_tools` placeholder. The agent calls FastAPI; FastAPI owns the data.

## Version caveats

This SDK moves fast — pin your versions and check release notes:

- The **KB env var** is `STRANDS_KNOWLEDGE_BASE_ID`; older examples use
  `KNOWLEDGE_BASE_ID`. This project accepts both.
- The **Memory API** is migrating from `MemoryClient` toward
  `MemoryManager` / `MemorySessionManager` in newer samples. The
  `AgentCoreMemorySessionManager` integration used here is stable, but verify
  against your installed `bedrock-agentcore` version.
- **RetrieveAndGenerate `modelArn`**: the synthesised
  `foundation-model` ARN only works for on-demand models. For inference
  profiles (`us./eu./apac.` prefixes — including the default model here), set
  `KB_MODEL_ARN` to the inference-profile ARN or the call will fail.

## References

- AgentCore docs: https://docs.aws.amazon.com/bedrock-agentcore/
- Strands + AgentCore Memory: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/strands-sdk-memory.html
- Strands `retrieve` / KB workflow: https://strandsagents.com/docs/examples/python/knowledge_base_agent/
- Session manager (community docs): https://strandsagents.com/docs/community/session-managers/agentcore-memory/
