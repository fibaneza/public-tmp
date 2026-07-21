# Observability — OTel, Tracing, Logs, Alerts & Monitoring

> **Scope:** Seeing inside agents and model calls — OpenTelemetry/ADOT, AgentCore Observability
> and CloudWatch GenAI Observability, model-invocation logging, metrics, alarms, and cost/token
> tracking. Current as of July 2026.

## Concept

AgentCore Observability is the default, **OTel-compatible** telemetry layer, surfaced in
**Amazon CloudWatch GenAI Observability**. On Runtime, agents are **auto-instrumented** (LLM
calls, tool invocations, memory ops) with no extra code. For Bedrock calls outside Runtime,
**CloudWatch Application Signals** emits the same OTel **GenAI semantic-convention** attributes.
The whole story is: standard OTel in, CloudWatch (or any OTel backend) out.

## One-time account setup — enable Transaction Search first

Nothing shows up until you enable **CloudWatch Transaction Search**. It ingests 100% of spans as
structured logs and indexes a configurable percentage as trace summaries. Do this before you go
looking for traces (Well-Architected `AGENTPERF01-BP02`).

## Instrument agent code (Python, ADOT)

Add the ADOT distro and run under auto-instrumentation:

```text
# requirements.txt
aws-opentelemetry-distro>=0.10.0
boto3
```

```bash
opentelemetry-instrument python -m app.runtime
```

This auto-instruments Strands / LangChain / LangGraph / CrewAI, Bedrock calls, tools, and DBs,
and ships traces to CloudWatch. Supported instrumentation libraries: **OpenInference,
OpenLLMetry, OpenLit, Traceloop**. The repo's Dockerfile pattern already wraps the entrypoint in
`opentelemetry-instrument` (see [`02-agentcore-runtime.md`](02-agentcore-runtime.md)).

**Don't use the ADOT *Collector*** for agent observability — it's unsupported here. Use the ADOT
**SDK** (or, for Lambda-hosted agents, the **AWS Lambda Layer for OpenTelemetry** with
`AWS_LAMBDA_EXEC_WRAPPER=/opt/otel-instrument`).

## Correlate a session and a whole request

Link every span in a conversation via OTel **baggage**, and propagate **W3C Trace Context**
across every agent→agent / agent→tool hop so one request is one connected trace, not fragments:

```python
from opentelemetry import baggage, context
ctx = baggage.set_baggage("session.id", session_id)
# attach ctx around the invocation so all child spans inherit session.id
```

Align LLM spans to the OTel **GenAI semantic conventions**: `gen_ai.request.model`,
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
`gen_ai.request.temperature/max_tokens/top_p`, `gen_ai.response.finish_reasons`. Standard
attributes make cross-model cost/latency comparison possible in Transaction Search.

## Model-invocation logging (off by default)

Captures full request/response/metadata for `Converse`, `ConverseStream`, `InvokeModel`,
`InvokeModelWithResponseStream` to **CloudWatch Logs and/or S3** (data > 100 KB or binary goes to
S3). Required for the GenAI Observability content view.

```python
boto3.client("bedrock").put_model_invocation_logging_configuration(loggingConfig={
    "cloudWatchConfig": {"logGroupName": "/aws/bedrock", "roleArn": ROLE_ARN,
        "largeDataDeliveryS3Config": {"bucketName": "bedrock-logs-big"}},
    "s3Config": {"bucketName": "bedrock-logs"},
    "textDataDeliveryEnabled": True,
    "imageDataDeliveryEnabled": True,
    "embeddingDataDeliveryEnabled": False})
```

These logs contain prompts/responses — apply **CloudWatch Logs data-protection masking** and KMS
encryption (see [`07-guardrails-and-pii.md`](07-guardrails-and-pii.md)).

## Metrics & alarms

Bedrock's CloudWatch namespace exposes: `Invocations`, `InvocationLatency`, `InputTokenCount`,
`OutputTokenCount`, `InvocationClientErrors`, `InvocationServerErrors`, **`InvocationThrottles`**.
Build dashboards and alarm on:

- **`InvocationThrottles`** — a capacity signal; a rising trend means you need CRIS/Provisioned
  Throughput.
- **error rates** (`InvocationServerErrors`/`ClientErrors`).
- **token spend** — alarm on `InputTokenCount`/`OutputTokenCount` anomalies.

Use **EventBridge** for event-driven reactions and **CloudTrail** for the audit trail.

## Cost & token tracking

Read `usage.inputTokens` / `usage.outputTokens` from the Converse response, plus
`cacheReadInputTokenCount` / `cacheWriteInputTokenCount` when prompt caching is on, to attribute
spend per route/user. Aggregate from model-invocation logs, tag resources, and set **AWS
Budgets**. Track cache hit rate as a first-class metric — it's the payoff of the caching work in
[`01-architecture-and-design.md`](01-architecture-and-design.md).

## Third-party backends

Telemetry is OTel-standard, so it integrates with Langfuse, Datadog, Dynatrace, Arize Phoenix,
LangSmith, Braintrust, IBM Instana, etc. The starter toolkit enables AgentCore OTel **by
default** — set **`disable_otel=True`** when routing to a third-party backend so you don't have
two exporters fighting.

## Do

- Enable Transaction Search before expecting traces.
- Run agents under `opentelemetry-instrument`; propagate W3C trace context + `session.id`
  baggage.
- Turn on model-invocation logging day one; mask PII and encrypt the destination.
- Alarm on throttles, errors, and token-spend anomalies; track cache hit rate.
- Follow OTel GenAI semantic conventions for portable, comparable spans.

## Don't

- Don't use the ADOT Collector for agent observability — use the SDK or Lambda layer.
- Don't run AgentCore OTel *and* a third-party exporter simultaneously — `disable_otel=True` for
  the latter.
- Don't ignore `InvocationThrottles` — it's your capacity early-warning.
- Don't log unmasked prompts/responses to an unencrypted store.

## Sources

- [Observability — configure](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html) · [get started (Transaction Search)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-get-started.html)
- [Model-invocation logging](https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html) · [`put_model_invocation_logging_configuration`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock/client/put_model_invocation_logging_configuration.html)
- [CloudWatch GenAI Observability](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/GenAI-observability.html) · [Application Signals GenAI scenario](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Services-example-scenario-GenerativeAI.html)
- [AgentCore Observability with Langfuse](https://aws.amazon.com/blogs/machine-learning/amazon-bedrock-agentcore-observability-with-langfuse/)
- Well-Architected Agentic AI Lens — [`AGENTOPS05-BP01`](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops05-bp01.html) · [`AGENTPERF01-BP02`](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentperf01-bp02.html)
