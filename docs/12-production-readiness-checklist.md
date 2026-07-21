# Production-Readiness Checklist & Standards

> **Scope:** One consolidated checklist across every concept in this set. Each item links to the
> file that explains it. Treat these as the enforceable standard for shipping a Bedrock +
> AgentCore workload; the prose is elsewhere. Current as of July 2026.

## Architecture & design → [`01`](01-architecture-and-design.md)

- [ ] Deterministic logic lives behind a testable API; the agent reaches data through tools, not
      a direct DB connection.
- [ ] Model chosen per route by evaluation (smallest that passes); Converse API used, not
      `invoke_model`.
- [ ] Prompt caching enabled on stable prefixes; cache hit rate tracked.
- [ ] New agents built on AgentCore + a framework — **not** Bedrock Agents "Classic"
      (closes to new customers 2026-07-30).
- [ ] Both Well-Architected lenses (Generative AI, Agentic AI) reviewed before go-live.

## Runtime → [`02`](02-agentcore-runtime.md)

- [ ] Entrypoint is thin; agent built in a testable factory.
- [ ] Image is **ARM64**, binds `0.0.0.0:8080`, implements `/invocations` + `/ping`.
- [ ] `context.session_id` read from the runtime, not the payload; compute treated as ephemeral.
- [ ] Long-running work uses the async-task pattern; the 8-hour / 15-minute limits are accounted
      for.
- [ ] Large artifacts staged in S3, not forced through the 100 MB payload.

## Session & memory → [`05`](05-session-and-memory.md)

- [ ] Unique session id (≥33 chars) per conversation; **session→user mapping enforced in your
      backend**, with a per-user session cap.
- [ ] `actorId` derived from the authenticated principal, validated before trust.
- [ ] Memory strategies match use cases; namespaces match the provisioned `namespaceTemplates`.
- [ ] Provisioning of Memory/KB is a run-once step, out of the request path.
- [ ] UI/logic tolerates async LTM consolidation lag (STM used for immediate continuity).

## RAG & knowledge bases → [`06`](06-rag-and-knowledge-bases.md)

- [ ] Chunking and embedding model locked deliberately before first ingest.
- [ ] Incremental sync automated (EventBridge); ingestion-failure alarm set.
- [ ] Complex queries decomposed; parallel retrieval uses **per-thread** boto3 clients.
- [ ] Over-retrieve → rerank; hybrid search used where the store supports it.
- [ ] Citations surfaced to users; tenant isolation enforced by a **non-optional** retrieval
      filter.
- [ ] Every RAG change gated behind an evaluation run (citation precision/coverage).

## Guardrails & PII → [`07`](07-guardrails-and-pii.md)

- [ ] `PROMPT_ATTACK` enabled on input; contextual grounding enabled for RAG.
- [ ] Guardrail versioned and **pinned** (never `DRAFT`) in production.
- [ ] PII policy set: ANONYMIZE for support/summarization, BLOCK for secrets/cards; custom regex
      for org IDs.
- [ ] Synchronous streaming used where PII masking is required (async can't mask).
- [ ] `ApplyGuardrail` used to pre-screen RAG inputs / retrieved chunks where warranted.

## Security → [`08`](08-security-roles-and-permissions.md)

- [ ] IAM scoped to specific ARNs; **CRIS two-ARN** policy present with the streaming action.
- [ ] Execution-role trust policy hardened with `aws:SourceAccount` + `aws:SourceArn`.
- [ ] No toolkit-generated IAM in production; validated with IAM Access Analyzer; permission
      boundaries applied.
- [ ] Runtime deployed in a VPC; Bedrock reached via PrivateLink with an endpoint policy.
- [ ] CMK (KMS) on KBs, guardrails, the token vault, and logs; CloudTrail on.
- [ ] Model access granted per Region; Runtime locked to the gateway role if gateway-fronted.

## Authentication & authorization → [`09`](09-authentication-and-authorization.md)

- [ ] JWT validated at the edge (API Gateway authorizer); no AWS keys in clients.
- [ ] Enterprise IdP (Entra) federated via Cognito or wired to AgentCore inbound auth directly.
- [ ] WebSocket connections authenticated explicitly.
- [ ] Tenant/user context propagated via the authorizer `context` and the retrieval filter.

## Identity (agent) → [`04`](04-agentcore-identity.md)

- [ ] Inbound vs outbound auth separated; user tokens **never** forwarded downstream.
- [ ] Downstream secrets in Identity credential providers (KMS-backed vault), not in code.
- [ ] User-delegated flow where actions must respect end-user permissions; autonomous only where
      appropriate.

## Observability → [`10`](10-observability-tracing-logs-monitoring.md)

- [ ] CloudWatch **Transaction Search** enabled.
- [ ] Agents run under `opentelemetry-instrument`; W3C trace context + `session.id` baggage
      propagated.
- [ ] Model-invocation logging on, PII-masked, KMS-encrypted.
- [ ] Alarms on throttles, error rates, and token-spend anomalies; AWS Budgets set.
- [ ] Exactly one telemetry exporter (AgentCore OTel **or** third-party, not both).

## Deployment → [`11`](11-deployment.md)

- [ ] All resources in IaC (KB, data source, guardrail + version, logging config, roles).
- [ ] Alpha CDK construct versions pinned; stable CFN resources where alpha risk is
      unacceptable.
- [ ] One endpoint per environment; immutable versions used for rollback.
- [ ] Reproducible builds (`uv`, pinned `requirements.txt`).

## Standards summary

- **Least privilege, always** — scoped ARNs, no `*`, boundaries, Access Analyzer.
- **Statelessness by design** — durable state in Memory/S3; session→user mapping in your backend.
- **Separation of identity concerns** — inbound identity ≠ outbound credentials.
- **Observability from day one** — Transaction Search, GenAI semantic conventions, logging.
- **Evaluate before you change** — model swaps and RAG tuning gated by measurement.

## Sources

- Every item links to its topic file; each topic file carries its own `## Sources`.
- [Well-Architected Generative AI Lens](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/generative-ai-lens.html) · [Agentic AI Lens](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/)
- [AgentCore Runtime security best practices](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html)
