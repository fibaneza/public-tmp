# Amazon Bedrock + AgentCore (Python) — Best-Practices Documentation Set

A concept-organized reference for building production agents and RAG systems on **Amazon
Bedrock** and **Amazon Bedrock AgentCore** in **Python**, current as of July 2026. Every
topic file follows the same shape — **concept → recommended Python API/pattern → do's →
don'ts → sources** — and flags GA vs preview inline.

This set is opinionated: it states a default, names the rejected alternative, and says why.
Where a topic is already covered by an existing repo document, this set **cross-references it
rather than duplicating** (see [Related repo documents](#related-repo-documents)).

## How to read this set

Start with `01-architecture-and-design.md` for the shape of the system, then jump to whatever
concept you need. Files are independent; the numbering is a suggested reading order, not a
dependency chain.

| # | File | Covers |
|---|---|---|
| — | [`README.md`](README.md) | This index, the Python toolchain map, and the GA/preview status matrix. |
| 01 | [`01-architecture-and-design.md`](01-architecture-and-design.md) | Reference architectures (RAG chatbot, agentic RAG, gateway-fronted AgentCore), Well-Architected GenAI + Agentic lenses, model selection, cost optimization, MCP/A2A. |
| 02 | [`02-agentcore-runtime.md`](02-agentcore-runtime.md) | Hosting agents: `BedrockAgentCoreApp`, sync/streaming/async, limits, ARM64 contract, versions/endpoints, built-in tools. |
| 03 | [`03-agentcore-gateway.md`](03-agentcore-gateway.md) | Turning APIs/Lambda/OpenAPI/MCP into governed MCP tools; semantic tool discovery; inbound/outbound auth. |
| 04 | [`04-agentcore-identity.md`](04-agentcore-identity.md) | Inbound vs outbound auth, workload identity, the token vault, `@requires_access_token`/`@requires_api_key`. |
| 05 | [`05-session-and-memory.md`](05-session-and-memory.md) | Session isolation and lifecycle; short-term vs long-term memory strategies and namespaces. |
| 06 | [`06-rag-and-knowledge-bases.md`](06-rag-and-knowledge-bases.md) | Knowledge Bases (stores, embeddings, chunking, sync) and RAG technique (decomposition, parallelism, rerank, evaluation). |
| 07 | [`07-guardrails-and-pii.md`](07-guardrails-and-pii.md) | Guardrail policy types, native vs `ApplyGuardrail`, streaming, and PII BLOCK/ANONYMIZE + masking. |
| 08 | [`08-security-roles-and-permissions.md`](08-security-roles-and-permissions.md) | Least-privilege IAM, the CRIS two-ARN pattern, confused-deputy hardening, VPC/PrivateLink, KMS, AgentCore Policy. |
| 09 | [`09-authentication-and-authorization.md`](09-authentication-and-authorization.md) | Cognito, Entra ID federation, OAuth2/OIDC, API Gateway authorizers, identity→session/tenant mapping. |
| 10 | [`10-observability-tracing-logs-monitoring.md`](10-observability-tracing-logs-monitoring.md) | OTel/ADOT, CloudWatch GenAI Observability, Transaction Search, model-invocation logging, metrics, alarms, cost tracking. |
| 11 | [`11-deployment.md`](11-deployment.md) | Starter toolkit, `@aws/agentcore` CLI, CDK, CloudFormation, Terraform, CI/CD. |
| 12 | [`12-production-readiness-checklist.md`](12-production-readiness-checklist.md) | One consolidated `- [ ]` checklist across every concept. |

## Platform in one paragraph

**Amazon Bedrock** is the managed foundation-model platform: model inference (the
model-agnostic **Converse API**), **Knowledge Bases** (managed RAG), **Guardrails** (safety +
PII), model-invocation logging, and evaluation. **Amazon Bedrock AgentCore** (GA October
2025) is the separate, composable platform for *operating agents* — **Runtime** (serverless
hosting), **Gateway** (tools as MCP), **Identity** (auth + token vault), **Memory** (managed
short/long-term memory), **Observability** (OTel → CloudWatch), and built-in **Code
Interpreter** / **Browser** tools. The services are à la carte: you can host a Strands agent
on Runtime with no Memory, or put Gateway/Identity in front of an agent running on ECS/EKS.
AgentCore is **framework-agnostic** (Strands, LangGraph, CrewAI, LlamaIndex, …) and
**model-flexible** (Bedrock, Anthropic, OpenAI, Gemini, …).

## The Python toolchain

There are four ways into this platform from Python; use them together.

| Tool | Install | What it gives you |
|---|---|---|
| **`bedrock-agentcore`** (SDK) | `pip install bedrock-agentcore` | `BedrockAgentCoreApp` + `@app.entrypoint`, `MemoryClient`/`MemorySessionManager`, identity decorators (`@requires_access_token`, `@requires_api_key`). The in-agent runtime library. |
| **`bedrock-agentcore-starter-toolkit`** | `pip install bedrock-agentcore-starter-toolkit` | The `Runtime` class and the Python **`agentcore configure` / `launch` / `invoke`** CLI. Builds ARM64 images via CodeBuild → ECR and wires Identity + Observability. |
| **`@aws/agentcore`** (Node CLI) | `npm install -g @aws/agentcore` (Node 20+) | Newer CLI: `agentcore create/dev/deploy/invoke/status`, backed by `@aws/agentcore-cdk`. **CodeZip** builds need no Docker. The samples repo is migrating toward this; the Python toolkit remains widely used. |
| **`boto3`** | `pip install boto3` | Direct control/data-plane access — see the client map below. Everything the CLIs do is ultimately a boto3 call. |

For the agent framework itself this set leads with **Strands** (`pip install strands-agents
strands-agents-tools`) — AWS's reference SDK for AgentCore and this repo's default — and shows
**LangGraph** and **CrewAI** equivalents for the key patterns.

### boto3 client map (memorize this first)

| Client | Plane | Key methods |
|---|---|---|
| `boto3.client("bedrock")` | Bedrock control | `create_guardrail`, `create_guardrail_version`, `put_model_invocation_logging_configuration`, `create_evaluation_job`, `list_foundation_models` |
| `boto3.client("bedrock-runtime")` | Bedrock inference | `converse`, `converse_stream`, `invoke_model`, `invoke_model_with_response_stream`, `apply_guardrail` |
| `boto3.client("bedrock-agent")` | KB/agent build | `create_knowledge_base`, `create_data_source`, `start_ingestion_job`, `get_ingestion_job` |
| `boto3.client("bedrock-agent-runtime")` | KB/agent runtime | `retrieve`, `retrieve_and_generate`, `retrieve_and_generate_stream`, `rerank`, `generate_query` |
| `boto3.client("bedrock-agentcore")` | AgentCore data | `invoke_agent_runtime`, `create_event`, `list_events`, `retrieve_memory_records` |
| `boto3.client("bedrock-agentcore-control")` | AgentCore control | `create_agent_runtime`, `create_gateway`, `create_memory`, `create_*_credential_provider` |

**Prefer the Converse API** (`converse`/`converse_stream`) over `invoke_model`: one
model-agnostic message schema, native tool use, and native guardrail integration, so you write
the code once and swap `modelId`.

## GA / preview status matrix

Flagged so you don't build on shifting ground. "GA" = generally available; "alpha" =
experimental API surface that can change.

| Capability | Status | Note |
|---|---|---|
| AgentCore Runtime / Gateway / Identity / Memory / Observability | **GA** | GA October 2025 (preview July 2025). VPC support, A2A, MCP connectivity, 8-hour runtimes. |
| AgentCore Code Interpreter / Browser tools | **GA** | Built-in sandboxed tools. |
| AgentCore Evaluations | **GA** | GA March 2026. |
| AgentCore Policy (Cedar, natural-language authoring) | **GA** | GA March 2026. |
| AgentCore Harness / Payments | **Newer** | Newest services — verify availability in your Region. |
| CDK L2 constructs for AgentCore (`aws_cdk.aws_bedrock_agentcore_alpha`) | **alpha** | Experimental; pin the version. CloudFormation `AWS::BedrockAgentCore::*` is the stable substrate. |
| Bedrock Knowledge Bases (incl. **S3 Vectors** store, **GraphRAG**) | **GA** | S3 Vectors ≈90% cheaper for infrequent-query RAG; GraphRAG uses Neptune Analytics. |
| Bedrock Guardrails (content, topics, words, PII, grounding, Automated Reasoning) | **GA** | Guardrails **async streaming cannot mask PII** — see `07`. |
| Bedrock RAG evaluation / LLM-as-a-judge | **GA** | GA March 2025. |
| Prompt caching, Cross-Region Inference (CRIS) | **GA** | CRIS needs a **two-ARN** IAM policy — see `08`. |
| Bedrock CDK L2 (`aws-bedrock-alpha`, `generative-ai-cdk-constructs`) | **alpha / deprecating** | `generative-ai-cdk-constructs` is migrating into `@aws-cdk/aws-bedrock-alpha`. |
| Bedrock **Agents "Classic"** (`bedrock-agent` `create_agent`) | **Closing** | **Closes to new customers 2026-07-30.** Build new agents on AgentCore + a framework, not Agents Classic. |

## Related repo documents (cross-referenced, not duplicated)

- [`../aws_kb.md`](../aws_kb.md) — Knowledge Base **metadata & file-structure** mechanics
  (`.metadata.json` sidecars, reserved fields, `includeForEmbedding`). `06` links here rather
  than repeating it.
- [`../Plan/architecture-v2.md`](../Plan/architecture-v2.md) — a concrete **gateway-fronted
  AgentCore** target architecture with a Mermaid diagram and a decision table. `01` builds on it.
- [`../Plan/agentcore-vs-backend.md`](../Plan/agentcore-vs-backend.md) — the **deterministic
  vs agentic** split (FastAPI vs AgentCore) and Strands memory/RAG pseudocode. `01`, `05`, `06`
  reference it.
- [`../agentcore`](../agentcore) — a React → Cognito → Gateway → Runtime **workflow diagram**.
- [`../Plan/`](../Plan/) `runtime.py`, `agent.py`, `memory.py`, `memory_hooks.py`, `kb.py` — a
  minimal, production-shaped **Strands-on-AgentCore** reference implementation used as the
  Strands examples throughout this set.

## Conventions in this set

- Code fences always carry a language hint (` ```python `, ` ```bash `, ` ```json `,
  ` ```mermaid `). Snippets elide error handling and IAM unless the point *is* IAM.
- Region and model IDs are illustrative; substitute your own and confirm **model access** is
  granted per Region.
- "Do / Don't" lists are the enforceable part — the prose explains why.

## Sources

- [What is Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html)
- [Amazon Bedrock AgentCore is now generally available](https://aws.amazon.com/about-aws/whats-new/2025/10/amazon-bedrock-agentcore-available/)
- [Develop agents (Python SDK + `@aws/agentcore` CLI)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/develop-agents.html)
- [`bedrock-agentcore` Python SDK](https://github.com/aws/bedrock-agentcore-sdk-python) · [starter toolkit](https://github.com/aws/bedrock-agentcore-starter-toolkit) · [samples](https://github.com/awslabs/amazon-bedrock-agentcore-samples)
- [Amazon Bedrock User Guide](https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html) · [Converse API](https://docs.aws.amazon.com/bedrock/latest/userguide/converse-api.html)
- [AWS Well-Architected Generative AI Lens](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/generative-ai-lens.html) · [Agentic AI Lens](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/)
- [Amazon Bedrock Agents is closing to new customers (2026-07-30)](https://docs.aws.amazon.com/bedrock/latest/userguide/agents.html)
