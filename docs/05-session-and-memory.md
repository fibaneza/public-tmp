# Session Handling & Memory Management

> **Scope:** How AgentCore isolates conversations (sessions) and how it remembers across turns
> and across sessions (Memory). These are two different mechanisms that people conflate — keep
> them straight. Strands, LangGraph, and CrewAI patterns. Current as of July 2026 (GA).

---

## Part 1 — Session handling

### Concept

A **session** is one interaction context, keyed by a client-supplied **`runtimeSessionId`**
(recommended **≥ 33 characters**; the Runtime generates one on first invoke if you omit it).
Each session runs in its **own microVM** — isolated CPU, memory, and filesystem — which is
destroyed and sanitized on termination.

Two ids, two jobs — both scope memory, so keep them distinct:

- **`sessionId`** — the conversation / compute context ("*which* conversation").
- **`actorId`** — the entity: user, agent, or system ("*who*").

### Lifecycle

| State | Meaning | Trigger to leave |
|---|---|---|
| **Active** | Processing a request or a background task (`HealthyBusy` ping) | Work completes → Idle |
| **Idle** | Context retained, awaiting the next call | 15-min idle (default) or explicit stop → Terminated |
| **Terminated** | microVM destroyed, memory sanitized | 8-h max lifetime, `StopRuntimeSession`, or unhealthy |

A later call with the same `runtimeSessionId` after termination spins up a **fresh** microVM
(same lifecycle config). The id stays valid until the Runtime ARN is deleted.

### Invocation (Python)

```python
import boto3, json, uuid
rt = boto3.client("bedrock-agentcore")
rt.invoke_agent_runtime(
    agentRuntimeArn=agent_arn,
    runtimeSessionId=f"user-123-{uuid.uuid4()}",   # ≥33 chars; reuse for related turns
    payload=json.dumps({"prompt": "Tell me about AWS"}).encode(),
)
```

### The responsibility AgentCore does *not* take

**AgentCore does not map sessions to users for you.** You must, in your own backend:

- generate a unique session id per user/conversation and reuse it for related turns;
- enforce that a session id belongs to the authenticated user before invoking; and
- cap the number of sessions per user.

The repo's [`../Plan/runtime.py`](../Plan/runtime.py) shows the discipline: it resolves the
actor id from the authenticated principal (a custom runtime header) and only falls back to a
payload value, "never trust a client-supplied actor id in production without validating it."

### Session do / don't

- **Do** use a `uuid4`-based id ≥33 chars; reuse it for all turns of one conversation.
- **Do** treat compute/filesystem state as ephemeral; persist durable state to Memory or S3.
- **Don't** share one session id across users or unrelated conversations — it breaks isolation
  and pollutes memory.
- **Don't** rely on in-VM state surviving a 15-minute idle gap or the 8-hour boundary.

---

## Part 2 — Memory management

### Concept: two tiers

- **Short-term memory (STM)** — raw **events** (user/assistant messages, tool actions) written
  **synchronously** per session. Gives multi-turn continuity *now*.
- **Long-term memory (LTM)** — durable **insights** extracted **asynchronously** from events by
  configured **strategies**, retrievable across sessions via semantic search.

Memory is scoped by **`actorId` + `sessionId`**; LTM retrieval uses them as namespace template
variables. PII is ignored by default in LTM records.

Memory is a **managed service**, not tables in your database — reach it via the SDK, and the
ownership boundary is enforced by service isolation (see
[`../Plan/agentcore-vs-backend.md`](../Plan/agentcore-vs-backend.md)).

### Strategy types (choose per use case)

| Strategy | Extracts | Namespace example |
|---|---|---|
| `semanticMemoryStrategy` | Facts / knowledge | `/facts/{actorId}` |
| `summaryMemoryStrategy` | Running session summary | `/summaries/{actorId}/{sessionId}` |
| `userPreferenceMemoryStrategy` | Preferences / style | `/preferences/{actorId}` |
| `episodicMemoryStrategy` | Episodes / experiences | `/episodes/{actorId}` |
| `customMemoryStrategy` | Domain-specific (you pick the extraction model + prompts) | your design |

### Provision the memory resource (once, out of band)

Provisioning is a migration, not request-path code — do it once via a script/IaC/console, then
reference the id. The repo's `scripts/provision_memory.py` is exactly this run-once step.

```python
import boto3
control = boto3.client("bedrock-agentcore-control")
resp = control.create_memory(
    name="AppMemory",
    memoryStrategies=[
        {"semanticMemoryStrategy":      {"name": "Facts",
            "namespaceTemplates": ["/facts/{actorId}"]}},
        {"userPreferenceMemoryStrategy":{"name": "Prefs",
            "namespaceTemplates": ["/preferences/{actorId}"]}},
        {"summaryMemoryStrategy":       {"name": "Summaries",
            "namespaceTemplates": ["/summaries/{actorId}/{sessionId}"]}},
    ])
# poll get_memory(memoryId=...) until status == "ACTIVE"
```

### Two SDK layers (know which you're calling)

The memory API exists at two layers with **different names** — mixing them is a common error:

| Layer | Create | Retrieve LTM | Namespaces field |
|---|---|---|---|
| **boto3 low-level** (`bedrock-agentcore-control` / `bedrock-agentcore`) | `create_memory(memoryStrategies=…)` | `retrieve_memory_records(memoryId, namespace, searchCriteria={searchQuery, topK})` | `namespaceTemplates` (camelCase) |
| **high-level SDK** (`MemoryClient`) | `create_memory_and_wait(strategies=…, event_expiry_days=…)` | `retrieve_memories(memory_id, namespace, query)` | `namespaces` (snake_case) |

The provision snippet above uses the boto3 layer; the snippets below use `MemoryClient`. Pick one
layer per module and stay consistent.

### Write & retrieve — the framework-agnostic path (`MemoryClient`)

Any framework can call the SDK directly. STM writes are synchronous; LTM retrieval (~200 ms) is
`MemoryClient.retrieve_memories` — a convenience wrapper over the boto3 `retrieve_memory_records`.

```python
from bedrock_agentcore.memory import MemoryClient
client = MemoryClient(region_name="us-west-2")

client.create_event(memory_id, actor_id, session_id,
    messages=[("What's my seat preference?", "USER"), ("Aisle.", "ASSISTANT")])

turns = client.get_last_k_turns(memory_id, actor_id, session_id, k=5)     # STM
prefs = client.retrieve_memories(memory_id=memory_id,                     # LTM
    namespace=f"/preferences/{actor_id}", query="seat preference")
```

### Strands — declarative (recommended) vs hooks

**Declarative** is the default: hand a session manager to the `Agent` and save/load is owned by
the SDK. This is the repo's [`../Plan/memory.py`](../Plan/memory.py).

```python
from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig, RetrievalConfig)
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager)

cfg = AgentCoreMemoryConfig(
    memory_id=MEMORY_ID, actor_id=actor_id, session_id=session_id,
    retrieval_config={                       # bias what LTM gets pulled into context
        "/facts/{actorId}":       RetrievalConfig(top_k=10, relevance_score=0.3),
        "/preferences/{actorId}": RetrievalConfig(top_k=5,  relevance_score=0.7),
    })
session_manager = AgentCoreMemorySessionManager(cfg, region_name="us-west-2")
# agent = Agent(model=..., session_manager=session_manager)
```

Tune retrieval per namespace: preferences few and high-confidence (wrong preferences are worse
than none); facts broad and loose (breadth helps grounding). The **namespaces here must match
the `namespaceTemplates` used at provisioning** — change one, change the other.

**Hooks** are the escape hatch for custom load/save (multi-agent shared memory, bespoke context
assembly): Strands calls your hooks on `AgentInitializedEvent` / `MessageAddedEvent` and *you*
own the `create_event` / `get_last_k_turns` calls — and their correctness. See
[`../Plan/memory_hooks.py`](../Plan/memory_hooks.py). Prefer declarative unless you have a
concrete reason.

### LangGraph & CrewAI

**LangGraph has a native integration.** The `langgraph-checkpoint-aws` package provides
`AgentCoreMemorySaver`, a checkpointer whose `thread_id` maps to the AgentCore `session_id` and
`actor_id` to the actor — so short-term persistence is handled for you (no hand-rolled
`create_event`):

```python
from langgraph_checkpoint_aws import AgentCoreMemorySaver   # pip install langgraph-checkpoint-aws
from langgraph.prebuilt import create_react_agent

checkpointer = AgentCoreMemorySaver(MEMORY_ID, region_name="us-west-2")
graph = create_react_agent("bedrock_converse:us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                           tools=tools, checkpointer=checkpointer)
graph.invoke({"messages": [("human", prompt)]},
    config={"configurable": {"thread_id": session_id, "actor_id": actor_id}})  # both REQUIRED
```

For long-term memories, pull them with `MemoryClient.retrieve_memories` inside a `pre_model_hook`
and inject them into the prompt.

**CrewAI** has no first-class AgentCore memory manager today (verify per version) — back it with
`MemoryClient`: call `create_event` after each task and `retrieve_memories` before, keyed by the
same `(actor_id, session_id)`.

The underlying primitive is identical; only the integration point differs.

### The async caveat that bites people

LTM extraction **lags** event ingestion — a fact stated this turn may not be retrievable as a
long-term memory for a short while. Design for it: use **STM for immediate continuity**, and add
loading/fallback states while LTM consolidates. Don't expect to write a preference and
semantically retrieve it one turn later.

### Memory do / don't

- **Do** match strategy to use case; design hierarchical namespaces for isolation
  (`support/user/{actorId}` vs `support/shared/product-knowledge`).
- **Do** provision once and reference by id; keep provisioning out of the request path.
- **Do** monitor consolidation with `list_memories` / `retrieve_memory_records` and tune.
- **Don't** try to hand-write LTM records — they're extracted asynchronously by strategies.
- **Don't** reuse a `sessionId` across unrelated conversations, or put PII in namespaces
  expecting it to persist (PII is stripped by default).

## Sources

- [Runtime sessions & lifecycle](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)
- [AgentCore Memory](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html) · [get started](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-get-started.html) · [customer scenario](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-customer-scenario.html)
- [Memory strategy input (API)](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_MemoryStrategyInput.html)
- [AgentCore Memory: building context-aware agents](https://aws.amazon.com/blogs/machine-learning/amazon-bedrock-agentcore-memory-building-context-aware-agents/) · [long-term memory deep dive](https://aws.amazon.com/blogs/machine-learning/building-smarter-ai-agents-agentcore-long-term-memory-deep-dive/)
- [Python SDK — memory API reference](https://aws.github.io/bedrock-agentcore-starter-toolkit/api-reference/memory.html)
- Repo: [`../Plan/memory.py`](../Plan/memory.py) · [`../Plan/memory_hooks.py`](../Plan/memory_hooks.py) · [`../Plan/runtime.py`](../Plan/runtime.py)
