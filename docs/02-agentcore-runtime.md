# AgentCore Runtime

> **Scope:** Hosting an agent on AgentCore Runtime from Python — the entrypoint contract,
> sync/streaming/async execution, the container contract, limits, versions, and built-in
> tools. Framework examples in Strands, LangGraph, and CrewAI. Current as of July 2026 (GA).

## Concept

AgentCore Runtime is a **serverless, session-isolated** hosting environment that runs your
agent as a container on AWS-managed infrastructure. It is framework-agnostic and
model-flexible, speaks **MCP** and **A2A**, and gives **each session its own microVM**
(isolated CPU/memory/filesystem, destroyed and sanitized on termination). Fast cold starts for
chat, plus long-running async up to **8 hours**. Pricing is consumption-based on *active* CPU —
you generally are not billed while the model is thinking.

You wrap any agent in ~4 lines. The SDK stands up an HTTP server exposing **`/invocations`
(POST)** and **`/ping` (GET)** and handles the platform contract for you.

## The entrypoint — one wrap, three frameworks

The wrap is identical across frameworks; only the agent you build inside differs. `payload` is
the caller's JSON body; `context` carries runtime metadata (read `context.session_id` — do not
trust a session id from the payload).

### Strands (repo default)

```python
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

app = BedrockAgentCoreApp()
agent = Agent(model=BedrockModel(model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
              system_prompt="You are a helpful assistant.")

@app.entrypoint
def invoke(payload, context):
    return {"response": str(agent(payload["prompt"]))}

if __name__ == "__main__":
    app.run()   # serves /invocations + /ping on 0.0.0.0:8080
```

This mirrors the repo's [`../Plan/runtime.py`](../Plan/runtime.py) and
[`../Plan/agent.py`](../Plan/agent.py), which also resolve the actor id from the authenticated
principal and keep the entrypoint thin by delegating construction to a factory.

### LangGraph

```python
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from langgraph.prebuilt import create_react_agent

app = BedrockAgentCoreApp()
# use the bedrock_converse: prefix (ChatBedrockConverse) — reliable tool calling
graph = create_react_agent("bedrock_converse:us.anthropic.claude-sonnet-4-5-20250929-v1:0", tools=[])

@app.entrypoint
def invoke(payload, context):
    result = graph.invoke({"messages": [("user", payload["prompt"])]})
    return {"response": result["messages"][-1].content}

if __name__ == "__main__":
    app.run()
```

### CrewAI

```python
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from crewai import Agent, Crew, Task

app = BedrockAgentCoreApp()

@app.entrypoint
def invoke(payload, context):
    researcher = Agent(role="Researcher", goal="Answer the user",
                       backstory="Expert analyst",
                       llm="bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    task = Task(description=payload["prompt"], agent=researcher,
                expected_output="A concise answer")
    return {"response": str(Crew(agents=[researcher], tasks=[task]).kickoff())}

if __name__ == "__main__":
    app.run()
```

The takeaway: **the Runtime contract is the constant; the framework is a detail.** Pick the
framework your team knows; AgentCore does not care.

## Streaming (SSE)

Turn the entrypoint into an async generator and `yield`; the Runtime emits `text/event-stream`
with no extra wiring.

```python
@app.entrypoint
async def invoke(payload, context):
    async for event in agent.stream_async(payload["prompt"]):  # Strands
        yield event
```

For LangGraph use `graph.astream(...)`; for CrewAI, stream token callbacks. Filter or transform
events before yielding if you don't want to leak internal tool chatter to the client.

## Async / long-running work

For research runs, multi-agent fan-out, or anything past a chat turn, mark background work so
the platform keeps the session alive. The `/ping` endpoint reports `Healthy` or `HealthyBusy`.

```python
@app.async_task            # flips ping to HealthyBusy while running
async def long_job(...):
    ...

# or manage explicitly:
task_id = app.add_async_task("research")
# ... do work ...
app.complete_async_task(task_id)
```

Max session lifetime is **8 hours**; idle timeout is **15 minutes** (both configurable via
lifecycle settings).

## Request context

The entrypoint's `context` exposes request-scoped state — read the session id from here, not
the payload:

```python
@app.entrypoint
def invoke(payload, context):
    session_id = getattr(context, "session_id", None) or "local-session"
    ...
```

The SDK also surfaces `request_id`, request headers, and (with Identity) the workload access
token. See [`05-session-and-memory.md`](05-session-and-memory.md) for how session/actor ids
drive isolation and memory.

## The container contract (design around it)

| Constraint | Value | Consequence |
|---|---|---|
| Architecture | **ARM64 / Graviton only** | A hand-built `amd64` image fails to start. The CLI/toolkit builds ARM64 for you. |
| Host / port | `0.0.0.0:8080` | Bind exactly this; other ports are not exposed (multi-port "coming soon"). |
| Endpoints | `POST /invocations`, `GET /ping` | The SDK provides both; a hand-rolled server must implement them. |
| Payload | up to **100 MB** | Send base64 multimodal docs/images/audio inline — but large artifacts still belong in S3. |
| Filesystem | ephemeral microVM | Nothing survives idle/termination; persist to Memory or S3. Permission checks inside the single-user microVM always succeed — don't rely on `chmod` for isolation. |

Hand-rolled Dockerfile (only when *not* using the toolkit), abbreviated:

```dockerfile
FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app/ ./app/
EXPOSE 8080
CMD ["uv","run","opentelemetry-instrument","python","-m","app.runtime"]
```

## Versions & endpoints

Every configuration change creates an **immutable version**. The `DEFAULT` endpoint tracks the
latest; create custom endpoints (`CreateAgentRuntimeEndpoint`) per environment (dev/test/prod).
Updates are zero-downtime and roll back by pointing an endpoint at a prior version. See
[`11-deployment.md`](11-deployment.md).

## Built-in tools

- **Code Interpreter** — a sandboxed environment for the agent to run code (data analysis,
  transformations) without you hosting an execution sandbox.
- **Browser** — a managed headless browser tool for web tasks.

Both are adoptable independently and run isolated from your Runtime container.

## Do

- Keep the entrypoint thin — build the agent in a factory (as
  [`../Plan/agent.py`](../Plan/agent.py) does) so it is unit-testable.
- Read `context.session_id`; treat compute as ephemeral; persist durable state to Memory/S3.
- Use sync for chat turns and the async-task pattern for long/multi-agent runs.
- Let the toolkit/CLI build ARM64; pin `requirements.txt` and prefer `uv` for reproducibility.

## Don't

- Don't build `amd64` images or bind a port other than 8080.
- Don't trust a client-supplied session/actor id without validating it against the
  authenticated principal — it scopes memory access.
- Don't store long-term state on the microVM filesystem.
- Don't treat the 100 MB payload ceiling as a data channel — stage large data in S3.

## Sources

- [Runtime — how it works](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html)
- [Runtime HTTP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html)
- [Runtime sessions & lifecycle](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)
- [Long-running agents](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-long-run.html)
- [Securely launch and scale agents on AgentCore Runtime](https://aws.amazon.com/blogs/machine-learning/securely-launch-and-scale-your-agents-and-tools-on-amazon-bedrock-agentcore-runtime/)
- [Python SDK — runtime API reference](https://aws.github.io/bedrock-agentcore-starter-toolkit/api-reference/runtime.html)
- Repo: [`../Plan/runtime.py`](../Plan/runtime.py) · [`../Plan/agent.py`](../Plan/agent.py)
