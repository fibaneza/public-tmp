# Security — Roles, Permissions & Data Protection

> **Scope:** IAM least-privilege for Bedrock and AgentCore, the CRIS two-ARN pattern,
> confused-deputy hardening, network isolation (VPC/PrivateLink), encryption (KMS), and
> AgentCore Policy. End-user auth is in
> [`09-authentication-and-authorization.md`](09-authentication-and-authorization.md). Current
> as of July 2026.

## 1. Least-privilege IAM for Bedrock

Scope actions **and** resources; avoid `Resource: "*"` in production.

- **Inference:** `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream` (Converse
  streams internally, so grant both), scoped to specific `foundation-model/*` ARNs.
- **KB build:** `bedrock:CreateKnowledgeBase`, `Get/List/Update/DeleteKnowledgeBase`,
  `StartIngestionJob`, scoped to `knowledge-base/<id>`.
- **KB runtime:** `bedrock:Retrieve`, `bedrock:RetrieveAndGenerate`.

### The CRIS two-ARN pattern (the #1 AccessDenied cause)

Cross-Region Inference profile IDs (e.g. `us.anthropic.claude-...`) require **two** statements
with **different ARN shapes** — get this wrong and every call 403s:

```json
{ "Version": "2012-10-17", "Statement": [
  { "Sid": "InferenceProfile", "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": "arn:aws:bedrock:*:123456789012:inference-profile/us.anthropic.claude-*" },
  { "Sid": "FoundationModels", "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": "arn:aws:bedrock:*::foundation-model/anthropic.claude-*" } ] }
```

Note the difference: the **inference-profile ARN includes the account id**; the
**foundation-model ARN has an empty account** (`::`). Both need the streaming action.

## 2. KB service role

A custom role trusting `bedrock.amazonaws.com`, granting only: invoke the embedding model, read
the data source (S3, etc.), access the specific vector store (OpenSearch data-access policy /
Aurora / the Secrets Manager secret), and `kms:Decrypt` for any CMK. A policy can't be shared
across roles — one role per KB.

## 3. AgentCore Runtime execution role

### Confused-deputy-hardened trust policy

```json
{ "Version": "2012-10-17", "Statement": [{
  "Effect": "Allow",
  "Principal": { "Service": "bedrock-agentcore.amazonaws.com" },
  "Action": "sts:AssumeRole",
  "Condition": {
    "StringEquals": { "aws:SourceAccount": "123456789012" },
    "ArnLike": { "aws:SourceArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:*" } } }] }
```

### Permissions

Minimally: ECR image pull (`ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`), CloudWatch Logs
on `/aws/bedrock-agentcore/runtimes/*`, plus whatever the agent actually calls (e.g.
`bedrock:InvokeModel`, `bedrock:Retrieve`, the metadata write path).

**Do not ship CLI/toolkit-generated IAM policies to production** — they are broad, for dev/test.
Author least-privilege policies scoped to specific Runtime ARNs, and ensure the execution role
has **≤** the privileges of the principals allowed to invoke it (prevents privilege
escalation). Apply permission boundaries and validate with **IAM Access Analyzer**.

## 4. Identity layering (Well-Architected Agentic AI Lens)

Distinguish four identities and enforce each at the right layer:

| Identity | What it is | Enforced by |
|---|---|---|
| **Agent identity** | The workload ARN | AgentCore Identity |
| **Service identity** | The IAM execution role | IAM policy + permission boundary |
| **Transaction identity** | Per-invocation scope | STS session policies / tags |
| **User identity** | Token claims | Authorization at the resource |

For per-tool credential isolation, call `sts:AssumeRole` / `AssumeRoleWithWebIdentity` inside
the agent and hand each MCP client scoped credentials — so one compromised tool can't use the
agent's full role.

## 5. Lock the front door

If you front the Runtime with a Gateway for governance (Guardrails, interceptors), the controls
are only real if the Runtime can't be reached directly:

- Set Runtime inbound auth to **IAM (SigV4)** and attach a **resource-based policy** admitting
  only the gateway's execution role, **or** use `allowedWorkloadConfiguration` for JWT.
- Remember for cross-account/resource policies that **both** the runtime and its endpoint must
  allow the action, and explicit `Deny` always wins.
- Restrict `bedrock-agentcore:InvokeAgentRuntimeForUser` to trusted principals; explicitly
  **Deny** user-id delegation where you don't need it.

## 6. Credential exposure inside the microVM

Any code in the microVM can read the execution-role credentials from the instance metadata
endpoint (MMDS). Treat **all in-agent code and tools as capable of using the role** — scope it
tightly, and don't run untrusted tool code with a broad role.

## 7. Network isolation

- **VPC deployment** for the Runtime; constrain with condition keys
  `bedrock-agentcore:subnets` and `bedrock-agentcore:securityGroups`.
- **Interface VPC endpoints (PrivateLink)** for Bedrock so traffic never traverses the
  internet; attach an **endpoint policy** to constrain principals/actions (e.g.
  `aws:PrincipalOrgID` for a data perimeter). KBs reach OpenSearch Serverless privately via
  PrivateLink.

## 8. Encryption & data residency

- **KMS customer-managed keys** for KBs, guardrails, the Identity token vault, model-
  customization artifacts, and logs.
- Inputs/outputs are **not** shared with model providers or used to train base models.
- Enable Cross-Region Inference only within geographies that meet your residency requirements;
  otherwise pin single-Region models.
- **CloudTrail** logs every control-plane call for audit.

## 9. Model access & abuse detection

Foundation models are **opt-in per account/Region** — grant via **Model access** in the console
before first use, or you get `AccessDeniedException`. Bedrock runs automated abuse detection on
usage.

## 10. AgentCore Policy (Cedar) — GA March 2026

A managed authorization engine using **Cedar**, with natural-language authoring. Use it to
express fine-grained "which agent/user may call which tool/resource under which conditions"
rules **outside** the agent code, evaluated at the Gateway/entry layer — so authorization is
declarative and auditable rather than scattered through prompts and tool wrappers.

## Do

- Scope every ARN; use the two-statement CRIS policy with the streaming action.
- Harden the execution-role trust policy with `aws:SourceAccount` + `aws:SourceArn`.
- Replace CLI-generated IAM with least-privilege policies; add STS session policies for
  per-tool isolation; validate with Access Analyzer.
- PrivateLink + endpoint policy; CMK everywhere; `aws:PrincipalOrgID` perimeter.
- Lock the Runtime to the gateway role if the gateway is your governed door.

## Don't

- Don't put an account id in a `foundation-model` ARN (or omit it from an `inference-profile`
  ARN).
- Don't ship toolkit-generated IAM to production, or use `Resource: "*"`.
- Don't grant `InvokeAgentRuntimeForUser` broadly or leave user-id delegation on by default.
- Don't assume gateway controls protect a Runtime that's still directly invocable.
- Don't run untrusted tool code with a broad execution role — it can read the role from MMDS.

## Sources

- [Runtime security best practices](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html) · [Runtime IAM permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)
- [Getting started with cross-Region inference (IAM)](https://aws.amazon.com/blogs/machine-learning/getting-started-with-cross-region-inference-in-amazon-bedrock/)
- [KB service-role permissions](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-permissions.html) · [KB user permissions](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-prereq-permissions-general.html)
- [Bedrock interface VPC endpoints](https://docs.aws.amazon.com/bedrock/latest/userguide/vpc-interface-endpoints.html) · [PrivateLink walkthrough](https://aws.amazon.com/blogs/machine-learning/use-aws-privatelink-to-set-up-private-access-to-amazon-bedrock/)
- [Secure AI agent access to AWS resources with MCP](https://aws.amazon.com/blogs/security/secure-ai-agent-access-patterns-to-aws-resources-using-model-context-protocol/)
- Well-Architected Agentic AI Lens — [`AGENTSEC03-BP03`](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentsec03-bp03.html)
- [AgentCore Policy GA](https://aws.amazon.com/about-aws/whats-new/2026/03/policy-amazon-bedrock-agentcore-generally-available/)
