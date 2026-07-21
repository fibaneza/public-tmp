# Deployment — AgentCore & Bedrock IaC

> **Scope:** Getting agents and Bedrock resources into an account — the AgentCore starter
> toolkit and CLIs for the fast path, and CDK / CloudFormation / Terraform for repeatable
> production. Current as of July 2026.

## The two front-ends for agents

There are two deployment front-ends; both guarantee **ARM64** (Runtime runs on Graviton — an
`amd64` image fails).

### Python starter toolkit — `configure → launch → invoke`

Generates a Dockerfile, builds an ARM64 image with **CodeBuild**, pushes to **ECR**, creates the
Runtime, and wires Identity + Observability.

```python
from bedrock_agentcore_starter_toolkit import Runtime
rt = Runtime()
rt.configure(
    entrypoint="app/runtime.py",
    execution_role=role_arn,          # or auto_create_execution_role=True
    auto_create_ecr=True,
    requirements_file="requirements.txt",
    region=region,
    agent_name="customer_support_agent",
    authorizer_configuration={"customJWTAuthorizer": {
        "allowedClients": [cognito_client_id],
        "discoveryUrl": cognito_discovery_url}},
    # disable_otel=True,   # only if using a third-party observability backend
)
launch = rt.launch()                  # CodeBuild → ECR → Runtime
resp = rt.invoke({"prompt": "..."})
```

CLI equivalent (matches the repo's `agentcore launch --env ...` in
[`../Plan/README.md`](../Plan/README.md)):

```bash
agentcore configure --entrypoint app/runtime.py -er <role_arn>
agentcore launch --env BEDROCK_AGENTCORE_MEMORY_ID=mem-xxxx \
                 --env STRANDS_KNOWLEDGE_BASE_ID=kb-xxxx
agentcore invoke '{"prompt": "What is our refund policy?"}'
```

### Node CLI — `@aws/agentcore` (newer)

`agentcore create → dev → deploy → invoke → status`, backed by `@aws/agentcore-cdk`. Two build
types:

- **CodeZip** (default) — zips code to S3, **no Docker required**.
- **Container** — a custom base image for system dependencies.

```bash
agentcore create --name support --framework Strands --model-provider Bedrock --build CodeZip
agentcore dev                 # run locally
agentcore deploy
agentcore invoke '{"prompt":"..."}'
agentcore add memory --name AppMemory --strategies SEMANTIC   # add resources
```

The samples repo is migrating toward this CLI; the Python toolkit remains widely used. Pick one
per project and stay consistent.

## Bedrock resources as IaC

### CloudFormation resource types

- Bedrock: `AWS::Bedrock::KnowledgeBase`, `::DataSource`, `::Guardrail`, `::GuardrailVersion`,
  `::Agent`, `::AgentAlias`.
- AgentCore: `AWS::BedrockAgentCore::Runtime`, `::Gateway`, `::Memory` (the stable substrate
  under the CDK constructs).

Pinning a guardrail version needs a **separate `GuardrailVersion` resource** — the guardrail and
its version are two resources.

### CDK

Two current Python paths — both **alpha**, so pin the version:

- **`aws_cdk.aws_bedrock_alpha`** (core CDK alpha) — the strategic direction; L2s for
  Guardrail/KB/etc. are migrating here.
- **`cdklabs.generative_ai_cdk_constructs`** (PyPI `generative-ai-cdk-constructs`) — mature L2s,
  being deprecated toward the alpha but widely used today.

```python
from cdklabs.generative_ai_cdk_constructs import bedrock

guardrail = bedrock.Guardrail(self, "g", name="my-guardrails")
guardrail.add_pii_filter(type=bedrock.pii_type.General.ADDRESS,
                         action=bedrock.GuardrailAction.ANONYMIZE)

kb = bedrock.VectorKnowledgeBase(self, "kb", embeddings_model=...)
bedrock.S3DataSource(self, "ds", bucket=docs_bucket, knowledge_base=kb,
                     chunking_strategy=bedrock.ChunkingStrategy.HIERARCHICAL)
```

For AgentCore, the CDK L2 constructs (`aws_cdk.aws_bedrock_agentcore_alpha`, e.g.
`GatewayTarget`) are **experimental** — use them behind a pinned version, or drop to the
`AWS::BedrockAgentCore::*` CloudFormation resources for stability.

### Terraform

Native AWS provider resources: `aws_bedrockagent_knowledge_base`, `aws_bedrockagent_data_source`,
`aws_bedrockagent_agent`, `aws_bedrock_guardrail`,
`aws_bedrock_model_invocation_logging_configuration`. Or the AWS-maintained module
**`aws-ia/terraform-aws-bedrock`** (feature-flag booleans for KB/guardrails/OpenSearch).

## CI/CD & environments

- Use the toolkit/CLI for fast iteration; graduate to CDK/CFN/Terraform for repeatable prod.
- One **custom endpoint per environment** (dev/test/prod); lean on immutable Runtime versions
  for rollback (point the endpoint at a prior version).
- Put the KB **service role** in the same stack as the KB; deploy the guardrail **and** its
  version together; include the model-invocation-logging config in IaC.
- Pin `requirements.txt` and prefer `uv` for reproducible builds; keep provisioning of Memory/KB
  as **run-once** steps out of the request path (see [`05-session-and-memory.md`](05-session-and-memory.md)).

## Do

- Let the toolkit/CLI build ARM64; deploy prod with CDK/CFN/Terraform.
- IaC everything: KB, data source, guardrail + version, logging config, execution role.
- Pin alpha CDK construct versions; prefer the stable CloudFormation resources where alpha risk
  is unacceptable.
- Use per-environment endpoints and immutable versions for zero-downtime rollout/rollback.

## Don't

- Don't build `amd64` images.
- Don't ship toolkit-auto-created IAM to production (see [`08-security-roles-and-permissions.md`](08-security-roles-and-permissions.md)).
- Don't click-ops guardrails/KBs — they drift and can't be reviewed.
- Don't leave AgentCore OTel enabled when deploying with a third-party observability stack
  (`disable_otel=True`).
- Don't forget the separate `GuardrailVersion` resource when pinning a version in CFN.

## Sources

- [Get started with the AgentCore CLI (ARM64, build types)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-cli.html) · [develop agents](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/develop-agents.html)
- [Move agents from PoC to production](https://aws.amazon.com/blogs/machine-learning/move-your-ai-agents-from-proof-of-concept-to-production-with-amazon-bedrock-agentcore/)
- [AgentCore samples (IaC)](https://github.com/awslabs/amazon-bedrock-agentcore-samples) · [starter toolkit](https://github.com/aws/bedrock-agentcore-starter-toolkit)
- [CDK `aws-bedrock-alpha`](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-bedrock-alpha-readme.html) · [generative-ai-cdk-constructs samples](https://github.com/aws-samples/generative-ai-cdk-constructs-samples)
- [Terraform `aws-ia/terraform-aws-bedrock`](https://github.com/aws-ia/terraform-aws-bedrock) · [deploy KBs with Terraform](https://aws.amazon.com/blogs/machine-learning/deploy-amazon-bedrock-knowledge-bases-using-terraform-for-rag-based-generative-ai-applications)
- Repo: [`../Plan/README.md`](../Plan/README.md)
