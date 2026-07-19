# AgentCore Runtime vs Backend API

> **Status:** Architecture decision record (ADR-style guidance)
> **Scope:** Where to put deterministic business logic vs. agentic/AI
> logic when building on Amazon Bedrock AgentCore.
> **AgentCore reference:** GA since Oct 2025. Five composable services —
> Runtime, Gateway, Identity, Memory, Observability — plus built-in
> tools (Code Interpreter, Browser). Each is adoptable à la carte.

---

## TL;DR

Do **not** use AgentCore Runtime as a general-purpose backend for CRUD.
Keep deterministic business logic in a conventional API (FastAPI) and
reserve AgentCore for agentic workloads. The agent should reach your
data **through your APIs**, not by touching your application database
directly.

Two corrections to the original draft are worth calling out up front,
because they change the design (details below):

1. **AgentCore Memory is a managed service, not tables in your Aurora
   cluster.** The "put an `agent` schema next to your `app` schema"
   model only applies if you deliberately *self-manage* session storage
   instead of using AgentCore Memory. Mixing the two framings leads to
   the wrong ownership diagram.
2. **AgentCore Gateway is primarily the agent's *outbound* tool-access
   layer, not an inbound API gateway in front of Runtime.** It *can*
   also front the Runtime as a governed entry point, but those are two
   different roles and shouldn't be collapsed into one arrow.

---

## The core principle

Separate **deterministic** logic from **probabilistic** logic.

| Property            | Backend API (FastAPI)        | Agent (AgentCore)                 |
| ------------------- | ---------------------------- | --------------------------------- |
| Behavior            | Deterministic, repeatable    | Non-deterministic, model-driven   |
| Correctness         | Provable, unit-testable      | Evaluated, not proven             |
| Transactions        | ACID, first-class            | Should delegate to the API        |
| Failure mode        | Explicit errors              | Hallucination, drift, retries     |
| Change cadence      | Versioned releases           | Prompt/model/tool iteration       |

The reason this split matters: an agent's output is a *sample*, not a
guarantee. Anything that must be correct every time (money movement,
permission checks, referential integrity) belongs behind an interface
you can test and version. The agent orchestrates and reasons; it does
not *own* the business invariants.

---

## Responsibilities

### FastAPI (Backend API) — deterministic core

- CRUD for business entities, rules, and workflows
- User administration
- Authorization decisions and business-rule enforcement
- Database access and transactions (ACID boundaries live here)
- File uploads
- Reporting and batch jobs
- OpenAPI contract / documentation
- Business validation

### AgentCore — agentic layer

- **Runtime:** hosts the agent container, exposes an `/invocations`
  endpoint, session isolation, long-running execution (up to ~8h)
- **Orchestration:** LangGraph (or Strands, CrewAI, etc.)
- **Bedrock model invocation** and AI reasoning / chat APIs
- **Gateway:** turns your APIs, Lambda functions, and MCP servers into
  agent tools (MCP `listTools` / `invokeTool`)
- **Memory:** managed short-term (session) and long-term memory
- **RAG / Knowledge Bases**
- **Identity:** inbound auth (who may call the agent) and outbound auth
  (agent accessing third-party/AWS resources via a token vault)

> **On auth:** the original draft placed *all* authentication in
> FastAPI. That's incomplete. AgentCore **Identity** exists precisely to
> handle agent-facing inbound auth and per-actor/per-target outbound
> credentials. Your IdP (Cognito/Okta/Entra) remains the source of
> truth for *user* identity; Identity handles *agent* trust and token
> exchange. FastAPI still enforces its own authZ on its own endpoints.

---

## Recommended architecture

There are two legitimate topologies. Pick based on whether you need a
single governed entry point in front of the agent.

### Pattern A — App calls Runtime directly (simplest)

```text
          React (SPA)
             │  (JWT from Cognito/IdP)
             ▼
     ┌───────────────┐        ┌──────────────────────────┐
     │  API Gateway  │        │  AgentCore Runtime        │
     │      +        │        │   ├── LangGraph orchestr. │
     │   FastAPI     │        │   ├── Bedrock models      │
     │               │        │   ├── Memory (managed)    │
     │  ├── Aurora   │◄───────┤   └── Identity (in/out)   │
     │  ├── S3       │  tools │            │              │
     │  └── Cognito  │  via   │            ▼              │
     └───────────────┘ Gateway│     AgentCore Gateway ────┘
                               │     (MCP: APIs/Lambda → tools)
                               ▼
                        Calls FastAPI endpoints
```

- Frontend calls **FastAPI** for deterministic operations and calls
  **AgentCore Runtime** (`/invocations`) for chat/agentic operations.
- The **agent** reaches business data by calling FastAPI endpoints —
  exposed to it as tools through **AgentCore Gateway** (which wraps your
  OpenAPI/Lambda targets as MCP tools). The agent never opens a DB
  connection to your application data.

### Pattern B — Gateway fronts the Runtime (governed entry point)

Use this when you want one policy-enforced door in front of the agent:
Guardrails, request/response interceptors, and unified observability
applied *outside* the agent's environment.

```text
   React ──► AgentCore Gateway ──► AgentCore Runtime ──► (tools via Gateway targets)
                    │
             policy / Guardrails / observability
```

If you do this, lock the Runtime's inbound auth to IAM (SigV4) and
attach a resource-based policy so **only the Gateway's execution role**
can invoke it — otherwise callers can bypass the governed door and hit
the Runtime directly.

> **Why the original single arrow was misleading:** `AgentCore Gateway →
> Runtime` describes Pattern B only. In everyday use, Gateway sits
> *after* the Runtime in the request flow — it's how the agent reaches
> tools. Both are real; label which one you mean.

---

## Data ownership

The instinct in the original — "AgentCore owns session data; FastAPI
shouldn't query it directly" — is correct. The mechanism was not.

| Component  | Owns                                                    |
| ---------- | ------------------------------------------------------- |
| FastAPI    | Users, business data, workflows, permissions            |
| AgentCore  | Sessions, conversation history, agent memory            |

**How that ownership is enforced depends on whether you use AgentCore
Memory:**

### If you use AgentCore Memory (recommended default)

Session/conversation/memory data lives in the **managed Memory
service**, not in your database at all. You read/write it via the SDK
(`MemoryClient`, e.g. `get_last_k_turns(...)`) or API. FastAPI, if it
ever needs session context, calls an AgentCore API (or a thin service
wrapper you own) — there is no shared table to accidentally couple to.

This is the cleaner option: the boundary is enforced by *service
isolation*, not by discipline.

### If you self-manage session storage instead

Only in this case does the "separate schema in the same Aurora cluster"
advice apply:

```text
Aurora
├── app                     ← owned by FastAPI
│   ├── users
│   ├── workflows
│   └── business_rules
└── agent                   ← owned by the agent service
    ├── sessions
    ├── messages
    └── memory
```

Rules if you go this route:

- No cross-schema SQL from `app` into `agent` (or vice versa). Cross a
  service boundary through an API, not a `JOIN`.
- Give each schema its own DB role with grants scoped to its own
  objects, so the coupling is *prevented*, not just discouraged.
- Expect to re-implement retention, consolidation, and
  short-term/long-term tiering that Memory gives you for free.

**Recommendation:** prefer AgentCore Memory unless you have a concrete
reason to self-host (e.g. a hard data-residency constraint Memory
doesn't yet satisfy, or an existing store you must reuse). Self-hosting
trades managed plumbing for control you probably don't need on day one.

---

## Analytics & observability

For dashboards and reporting, **do not query live session state**
(whether that's the Memory service or your `agent` schema). Instead:

- Emit AgentCore **Observability** telemetry (OTel traces/metrics) to
  CloudWatch, and/or export events into your own analytics tables or an
  observability platform.
- Report off that copy. This keeps analytical load off the operational
  path and gives you a stable schema to build dashboards on even as the
  agent's internal representation changes.

---

## Decision summary

- **Business logic → FastAPI.** Deterministic, testable, transactional.
- **Agentic logic → AgentCore Runtime.** Orchestration, reasoning, chat.
- **Agent → data via your APIs** (surfaced as tools through Gateway),
  never via direct DB access.
- **Sessions/memory → AgentCore Memory** (managed) by default; a
  separate Aurora schema only if you deliberately self-host.
- **Auth is shared:** IdP owns user identity, AgentCore Identity owns
  agent trust + outbound credentials, FastAPI enforces its own authZ.
- **Analytics off exported telemetry,** not live session state.

---

## References

- Amazon Bedrock AgentCore docs: https://docs.aws.amazon.com/bedrock-agentcore/
- AgentCore GA announcement (A2A, self-managed Memory, MCP targets):
  https://aws.amazon.com/about-aws/whats-new/2025/10/amazon-bedrock-agentcore-available
- Front your Runtime with a Gateway (inbound auth, resource policy):
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html
- Calling tools via the Gateway MCP endpoint:
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-using-mcp-call.html

---

## Appendix A — Strands implementation (no LangGraph)

Strands is AWS's reference SDK for AgentCore, so session, memory, and
Knowledge Base wiring is more first-class here than with LangGraph — you
mostly configure providers and let the SDK handle the plumbing. The
snippets below are **pseudocode**: real API names, elided error
handling and IAM setup.

> **Setup vs. runtime (important):** the Memory resource and the
> Knowledge Base are **provisioned once** (Console, IaC, or a setup
> script) — *not* on every agent invocation. Your agent code only
> references them by ID. Treat memory/KB creation like a migration, not
> request-path code.

### A.1 Sessions + Memory — the declarative path (recommended)

Use `AgentCoreMemorySessionManager`. It auto-loads prior context on
start and auto-saves each turn, so you don't hand-write hooks. On
AgentCore Runtime, `session_id` is supplied by the runtime
(`context.session_id`); locally you set it yourself.

```python
# --- provisioned ONCE, out of band (setup script / IaC) ---
# client = MemoryClient(region_name=REGION)
# memory = client.create_memory_and_wait(
#     name="AppMemory",
#     strategies=[                      # long-term memory strategies
#       {"userPreferenceMemoryStrategy": {"name": "Prefs",
#         "namespaces": ["/preferences/{actorId}"]}},
#       {"semanticMemoryStrategy":       {"name": "Facts",
#         "namespaces": ["/facts/{actorId}"]}},
#       {"summaryMemoryStrategy":        {"name": "Summaries",
#         "namespaces": ["/summaries/{actorId}/{sessionId}"]}},
#     ])
# MEMORY_ID = memory["id"]

# --- runtime (per request) ---
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig, RetrievalConfig)
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager)
from strands import Agent
from strands.models import BedrockModel

app = BedrockAgentCoreApp()

@app.entrypoint
def handle(payload, context):
    actor_id = payload["actor_id"]          # WHO — stable per user
    session_id = context.session_id         # WHICH conversation (runtime-provided)

    mem_cfg = AgentCoreMemoryConfig(
        memory_id=MEMORY_ID,
        actor_id=actor_id,
        session_id=session_id,
        # optional: bias what long-term memory gets pulled into context
        retrieval_config={
            "/facts/{actorId}":       RetrievalConfig(top_k=5, relevance_score=0.4),
            "/preferences/{actorId}": RetrievalConfig(top_k=3, relevance_score=0.5),
        },
    )
    session_manager = AgentCoreMemorySessionManager(mem_cfg, region_name=REGION)

    agent = Agent(
        model=BedrockModel(model_id=MODEL_ID),
        system_prompt="You are a helpful assistant.",
        session_manager=session_manager,    # ← save/load handled for you
    )
    return agent(payload["prompt"])         # history in, reply out, turn persisted

app.run()
```

Why this over hooks: fewer moving parts, no manual `create_event` /
`get_last_k_turns` calls to keep in sync, and it degrades cleanly — if
you later want custom retrieval you can drop to hooks without changing
the agent's public shape.

### A.2 Sessions + Memory — the hook path (when you need control)

Reach for this only when you need custom load/save logic (multi-agent
shared memory, bespoke context assembly, non-standard namespaces).
Strands calls your hooks at lifecycle points; you own the Memory calls.

```python
from strands import Agent
from strands.hooks import HookProvider, HookRegistry
from strands.hooks import AgentInitializedEvent, MessageAddedEvent
from bedrock_agentcore.memory import MemoryClient

class MemoryHook(HookProvider):
    def __init__(self, client, memory_id, actor_id, session_id):
        self.client, self.memory_id = client, memory_id
        self.actor_id, self.session_id = actor_id, session_id

    def register_hooks(self, registry: HookRegistry):
        registry.add_callback(AgentInitializedEvent, self.on_start)
        registry.add_callback(MessageAddedEvent, self.on_message)

    def on_start(self, event):
        # LOAD: recent turns (short-term) + relevant long-term memories
        turns = self.client.get_last_k_turns(
            memory_id=self.memory_id, actor_id=self.actor_id,
            session_id=self.session_id, k=5)
        facts = self.client.retrieve_memories(
            memory_id=self.memory_id, namespace=f"/facts/{self.actor_id}",
            query=event.agent.messages[-1] if event.agent.messages else "")
        event.agent.system_prompt += render_context(turns, facts)

    def on_message(self, event):
        # SAVE: persist each new message as it is added
        self.client.create_event(
            memory_id=self.memory_id, actor_id=self.actor_id,
            session_id=self.session_id, messages=[event.message])

client = MemoryClient(region_name=REGION)
agent = Agent(
    model=BedrockModel(model_id=MODEL_ID),
    hooks=[MemoryHook(client, MEMORY_ID, actor_id, session_id)],
)
```

Trade-off: maximum flexibility, but you now own correctness of the
save/load lifecycle. Prefer A.1 unless you have a concrete reason.

### A.3 Knowledge Base (RAG) — the `retrieve` tool

For a Bedrock Knowledge Base, don't hand-roll `bedrock-agent-runtime`
calls — hand the agent the built-in `retrieve` tool and let it decide
when to search. The KB ID comes from an env var; provision the KB (and
its S3 data source) out of band.

```python
# Provisioned once: Bedrock Knowledge Base backed by S3, auto-synced.
# At deploy time, pass the ID as an env var, e.g.:
#   agentcore launch --env STRANDS_KNOWLEDGE_BASE_ID=kb-xxxx
# (older examples read KNOWLEDGE_BASE_ID — the current strands_tools
#  retrieve tool uses STRANDS_KNOWLEDGE_BASE_ID; set both if unsure.)

from strands import Agent
from strands.models import BedrockModel
from strands_tools import retrieve          # semantic KB search over Bedrock KBs

agent = Agent(
    model=BedrockModel(model_id=MODEL_ID),
    system_prompt=(
        "Answer from the knowledge base. Use `retrieve` to look things up. "
        "Cite sources. If nothing relevant is found, say so — do not guess."
    ),
    tools=[retrieve],                        # KB_ID read from env at call time
)

reply = agent("What is our refund policy for enterprise plans?")
# The model calls retrieve(text=...) itself; results are grounded + citable.
```

If you need deterministic RAG (always retrieve, then answer — no model
discretion), call the tool explicitly in code instead of exposing it:

```python
hits = agent.tool.retrieve(text=user_query)  # forced retrieval
agent(f"Context:\n{hits}\n\nQuestion: {user_query}")
```

### A.4 Putting it together

All three compose on one agent — session/memory via the session manager,
KB via the tool, business actions via Gateway-wrapped API tools:

```python
agent = Agent(
    model=BedrockModel(model_id=MODEL_ID),
    system_prompt=SYSTEM_PROMPT,
    session_manager=session_manager,   # A.1 — conversation + long-term memory
    tools=[retrieve, *gateway_tools],  # A.3 KB search + your FastAPI-as-tools
)
```

Note the boundary the main document argued for: **`retrieve` reads the
KB, `gateway_tools` call FastAPI for business data — the agent still
never touches your application database directly.** Memory is the
managed service, not an Aurora schema.

> **Naming/version caveats to verify against your installed versions:**
> the KB env var (`STRANDS_KNOWLEDGE_BASE_ID` vs legacy
> `KNOWLEDGE_BASE_ID`), and the Memory API surface (the newer
> `MemoryManager` / `MemorySessionManager` are superseding the older
> `MemoryClient` in samples). Pin `strands-agents` /
> `strands-agents-tools` / `bedrock-agentcore` versions and check the
> release notes, since this SDK is moving fast.
