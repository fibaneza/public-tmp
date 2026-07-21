# Guardrails & PII Management

> **Scope:** Amazon Bedrock Guardrails (the six policy types, native vs independent evaluation,
> streaming) and PII handling (BLOCK vs ANONYMIZE, entity types, Comprehend, logging without
> PII). Current as of July 2026 (GA).

---

## Part 1 — Guardrails

### Concept

Guardrails is a **model-independent** safety layer you configure once and apply to any model
(Bedrock or external). Six policy types:

| Policy | Catches |
|---|---|
| **Content filters** | Hate, insults, sexual, violence, misconduct, and **prompt attack** (text + image) |
| **Denied topics** | Subjects you define as off-limits |
| **Word filters** | Custom words + managed profanity |
| **Sensitive-information filters** | PII (built-in entities) + custom regex — see Part 2 |
| **Contextual grounding** | Grounding + relevance checks (essential for RAG) |
| **Automated Reasoning** | Math-verified factuality against a policy model |

### Create a guardrail (Python)

```python
import boto3
bedrock = boto3.client("bedrock")

g = bedrock.create_guardrail(
    name="app-guardrail",
    topicPolicyConfig={"topicsConfig": [
        {"name": "Legal_Advice", "definition": "Advice requiring a lawyer",
         "type": "DENY", "inputAction": "BLOCK", "outputAction": "BLOCK"}]},
    contentPolicyConfig={"filtersConfig": [
        {"type": "PROMPT_ATTACK", "inputStrength": "HIGH", "outputStrength": "NONE"},
        {"type": "HATE", "inputStrength": "HIGH", "outputStrength": "HIGH"}]},
    contextualGroundingPolicyConfig={"filtersConfig": [
        {"type": "GROUNDING", "threshold": 0.75},
        {"type": "RELEVANCE", "threshold": 0.75}]},
    blockedInputMessaging="I can't help with that.",
    blockedOutputsMessaging="I can't provide that.")
bedrock.create_guardrail_version(guardrailIdentifier=g["guardrailId"])
```

`PROMPT_ATTACK` is set on **input only** — `outputStrength` must be `NONE` for it.

### Versioning

Work against `DRAFT` while testing; cut immutable numbered versions for production and **pin
`guardrailVersion`** in inference. Never point production at `DRAFT`.

### Two ways to apply

**1. Native (dual checkpoint).** Pass `guardrailConfig` to `converse`/`invoke_model`; Bedrock
checks input, then output. A block surfaces as `stopReason == "guardrail_intervened"`.

**2. `ApplyGuardrail` API (independent).** Evaluate *any* text with **no model invocation** —
so it works with self-hosted/third-party models, SageMaker, and agent frameworks, and lets you
vet RAG inputs *before* retrieval or filter retrieved chunks *before* generation.

```python
rt = boto3.client("bedrock-runtime")
rt.apply_guardrail(guardrailIdentifier=g["guardrailId"], guardrailVersion="1",
    source="INPUT", content=[{"text": {"text": user_prompt}}])
```

**Latency vs cost tradeoff:** you *can* run the input `ApplyGuardrail` call in parallel with
inference to cut latency — but you then pay for both even on a block. Sequential lets you skip
inference when the guardrail intervenes. Choose per risk profile; each `ApplyGuardrail` call is
billed separately.

### Streaming guardrails

`converse_stream` / `invoke_model_with_response_stream` support two modes via
`streamProcessingMode`:

- **Synchronous** — buffers and scans chunks before emitting (safer, adds latency).
- **Asynchronous** — emits immediately, scans in the background, blocks later chunks on
  detection. **Async does NOT support PII masking** — see Part 2.

### Tiers & debugging

Content/topic filters offer a **Standard tier** (more languages, stronger) vs **Classic**. Set
`outputScope: FULL` to return all detected *and* non-detected entries while debugging.

---

## Part 2 — PII management & masking

### Primary tool: the sensitive-information policy

Two actions per entity, configurable **independently for input vs output**
(`inputAction`/`outputAction`, `inputEnabled`/`outputEnabled`):

- **BLOCK** — reject the whole request/response with your message. Use for secrets and card
  numbers.
- **ANONYMIZE** — replace with a typed placeholder (`{EMAIL}`, `{NAME}`). Use for
  summarization/support where the text should survive with PII removed.

Masking applies to **both requests and responses**.

```python
bedrock.create_guardrail(name="pii-guard",
    sensitiveInformationPolicyConfig={
        "piiEntitiesConfig": [
            {"type": "EMAIL",
             "inputAction": "ANONYMIZE", "outputAction": "ANONYMIZE",
             "inputEnabled": True, "outputEnabled": True},
            {"type": "CREDIT_DEBIT_CARD_NUMBER",
             "inputAction": "BLOCK", "outputAction": "BLOCK",
             "inputEnabled": True, "outputEnabled": True}],
        "regexesConfig": [
            {"name": "BookingID", "pattern": r"^[A-Z]{2}\d{6}$", "action": "ANONYMIZE"}]},
    blockedInputMessaging="PII not allowed.",
    blockedOutputsMessaging="PII removed.")
```

### Entity coverage

Built-in types (BLOCK or ANONYMIZE) span **General** (`NAME`, `EMAIL`, `ADDRESS`, `AGE`,
`PHONE`, `USERNAME`, `PASSWORD`, `DRIVER_ID`, …), **Finance** (`CREDIT_DEBIT_CARD_NUMBER/CVV/
EXPIRY`, `PIN`, `INTERNATIONAL_BANK_ACCOUNT_NUMBER`, `SWIFT_CODE`), **IT** (`IP_ADDRESS`,
`MAC_ADDRESS`, `URL`, `AWS_ACCESS_KEY`, `AWS_SECRET_KEY`), and country-specific IDs (US SSN/ITIN/
passport, UK NHS/NINO/UTR, CA SIN, …). Add **custom regex** for org-specific identifiers.

Two limits to design around: it is a **text-only** ML detector (it won't catch PII inside a
`tool_use` result's structured params — give it surrounding context for accuracy), and
**asynchronous streaming can't mask** — use synchronous streaming when masking matters.

### Alternative / complement: Amazon Comprehend

When you need **deterministic, offset-based** redaction (not an LLM judgment) — e.g. a
pre-processing pipeline, or English/Spanish document redaction — use Comprehend
`detect_pii_entities` / `contains_pii_entities`, which return entity type, **character
offsets**, and confidence. It's also available as a LangChain moderation chain. Use Guardrails
in the inference path; add Comprehend where you need explicit offsets or non-LLM redaction.

### Logging without PII

Model-invocation logging captures full prompts/responses (see
[`10-observability-tracing-logs-monitoring.md`](10-observability-tracing-logs-monitoring.md)).
Pair it with **CloudWatch Logs data-protection masking policies** to mask sensitive fields in
logs, and encrypt log stores with **KMS**. Otherwise you've just moved the PII from the response
into your logs.

## Do

- Enable `PROMPT_ATTACK` on input; enable contextual grounding for RAG.
- Version and pin guardrails; use different configs per workflow stage (input screen, retrieved
  chunks, final output).
- ANONYMIZE for summarization/support; BLOCK for card numbers and secrets; add regex for
  org-specific IDs.
- Use `ApplyGuardrail` to pre-screen RAG inputs and retrieved chunks before generation.
- Mask PII in CloudWatch logs and encrypt log destinations.

## Don't

- Don't point production inference at a `DRAFT` guardrail.
- Don't use asynchronous streaming when you need PII masking (unsupported).
- Don't assume masking covers structured tool-call params, or that one guardrail fits every
  workflow stage.
- Don't log raw prompts/responses to an unmasked, unencrypted destination.

## Sources

- [Guardrails overview](https://aws.amazon.com/bedrock/guardrails/) · [sensitive-information filters](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-sensitive-filters.html)
- [ApplyGuardrail API](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-use-independent-api.html) · [streaming guardrails](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-streaming.html)
- [Guardrails best practices](https://aws.amazon.com/blogs/machine-learning/build-safe-generative-ai-applications-like-a-pro-best-practices-with-amazon-bedrock-guardrails/)
- [Tokenization + Guardrails for secure data handling](https://aws.amazon.com/blogs/machine-learning/integrate-tokenization-with-amazon-bedrock-guardrails-for-secure-data-handling/)
- [Comprehend `DetectPiiEntities`](https://docs.aws.amazon.com/comprehend/latest/APIReference/API_DetectPiiEntities.html) · [Comprehend + LangChain trust & safety](https://aws.amazon.com/blogs/machine-learning/build-trust-and-safety-for-generative-ai-applications-with-amazon-comprehend-and-langchain/)
