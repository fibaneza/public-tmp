# AgentCore Gateway

> **Scope:** Turning APIs, Lambda functions, OpenAPI/Smithy specs, and existing MCP servers
> into governed **MCP tools** for your agent — target types, semantic tool discovery, and
> inbound/outbound auth. Current as of July 2026 (GA).

## Concept

Gateway is a fully-managed **MCP server** that fronts your tools behind one secure endpoint.
It is the only managed option offering **both ingress (inbound) and egress (outbound)
authentication**, plus **semantic search over tools** so the model finds the right tool without
you stuffing every schema into the prompt. Two distinct roles (don't conflate them):

- **Outbound tool layer** (the common one) — the agent calls Gateway over MCP `tools/call`;
  Gateway invokes the downstream target with the right credentials.
- **Inbound governed entry** — Gateway sits in front of the Runtime as the policy-enforced
  door (Guardrails, interceptors, observability). See
  [`01-architecture-and-design.md`](01-architecture-and-design.md) and
  [`../Plan/architecture-v2.md`](../Plan/architecture-v2.md).

## Target types

| Target | Turns into tools from | Use when |
|---|---|---|
| `Lambda` | A Lambda function | You have serverless business logic already. |
| `OpenAPI` | An OpenAPI spec (inline or S3) | You have a documented REST API. |
| `Smithy` | A Smithy model | You model APIs with Smithy. |
| `MCP server` | An existing MCP server URL | You already run MCP servers and want them unified. |
| `API_GATEWAY` | An Amazon API Gateway REST API directly | You want to skip exporting/importing OpenAPI. |

## Create a gateway (boto3 control plane)

```python
import boto3
gw = boto3.client("bedrock-agentcore-control")

resp = gw.create_gateway(
    name="DemoGateway",
    roleArn="<gateway_execution_role_arn>",
    protocolType="MCP",
    protocolConfiguration={"mcp": {"supportedVersions": ["2025-03-26"],
                                   "searchType": "SEMANTIC"}},   # semantic tool discovery
    authorizerType="CUSTOM_JWT",
    authorizerConfiguration={"customJWTAuthorizer": {
        "allowedClients": ["<cognito_client_id>"],
        "discoveryUrl": "<oidc_discovery_url>"}},
)
gateway_url = resp["gatewayUrl"]   # the MCP endpoint your agent (or another agent) calls
```

Then add targets (`create_gateway_target`) with their credential configuration (below).

## Semantic tool discovery

With `searchType: SEMANTIC`, Gateway auto-embeds each tool's name/description/parameters at
sync time and exposes a built-in search tool, **`x_amz_bedrock_agentcore_search`** (called via
MCP `tools/call`). The agent searches for a capability in natural language and gets the
relevant tools back, so:

- the model's tool list stays small (better accuracy, lower token cost), and
- tools are found even when the user's wording doesn't match the tool name.

Enable it whenever a gateway hosts more than a handful of tools.

## Outbound (egress) auth — credential providers

Secrets never belong in tool schemas or agent code. Create a credential provider in Identity,
then attach it to a target. Provider types: **API key**, **OAuth**, and **IAM role** (default).

```python
acps = boto3.client("bedrock-agentcore-control")
cp = acps.create_api_key_credential_provider(name="PartnerAPIKey", apiKey="<secret>")

# When creating the target, reference the provider (abbreviated):
# credentialProviderConfigurations=[{
#   "credentialProviderType": "API_KEY",
#   "credentialProvider": {"apiKeyCredentialProvider": {
#       "providerArn": cp["credentialProviderArn"],
#       "credentialParameterName": "api_key",
#       "credentialLocation": "QUERY_PARAMETER"}}}]
```

For OAuth downstreams, use an OAuth credential provider so Gateway performs the token exchange
and refresh (backed by the KMS-encrypted token vault — see
[`04-agentcore-identity.md`](04-agentcore-identity.md)).

## Inbound (ingress) auth

Options on the gateway itself:

- **`CUSTOM_JWT`** — any OAuth2 IdP via its discovery URL. Verify with `allowedClients` *or*
  `allowedAudience` — prefer `allowedAudience` for IdPs (e.g. Okta) that place client identity
  in a non-standard claim like `cid`.
- **IAM (SigV4)** — for service-to-service and for the gateway-fronts-Runtime pattern.
- **Offloaded** ("authenticate only" / "no authorization") — defer authorization to the
  downstream target, a policy engine (AgentCore Policy), or an interceptor Lambda.

Keep the inbound authorizer's job narrow — *may this caller use the gateway* — and enforce
per-downstream authorization at the egress/target layer or with AgentCore Policy. For
multi-tenant "act on behalf of user" flows, use on-behalf-of token exchange rather than
forwarding the raw user token downstream.

## Do

- Enable `searchType: SEMANTIC` for any non-trivial tool set.
- Store every downstream secret in an Identity credential provider (Secrets Manager/KMS-backed).
- Use the `API_GATEWAY` target type for existing REST APIs instead of round-tripping OpenAPI.
- Separate an **entry** gateway (inbound user JWT) from a **tools** gateway (outbound
  credentials) so each resource's policy stays minimal — as
  [`../Plan/architecture-v2.md`](../Plan/architecture-v2.md) does.

## Don't

- Don't hardcode API keys in agent code or tool schemas.
- Don't rely on `allowedClients` with IdPs that use non-standard client claims — use
  `allowedAudience`.
- Don't forward the end user's inbound token to downstream services — exchange it.
- Don't assume the entry-gateway controls protect you if the Runtime is still directly
  reachable (lock it — see [`08-security-roles-and-permissions.md`](08-security-roles-and-permissions.md)).

## Sources

- [Gateway — building and adding targets](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-building-adding-targets.html)
- [Gateway inbound auth](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-inbound-auth.html) · [JWT authorizer](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/inbound-jwt-authorizer.html)
- [Introducing AgentCore Gateway](https://aws.amazon.com/blogs/machine-learning/introducing-amazon-bedrock-agentcore-gateway-transforming-enterprise-ai-agent-tool-development/)
- [Connect API Gateway to AgentCore Gateway with MCP](https://aws.amazon.com/blogs/machine-learning/streamline-ai-agent-tool-interactions-connect-api-gateway-to-agentcore-gateway-with-mcp/)
- [On-behalf-of token exchange for multi-tenant agents](https://aws.amazon.com/blogs/machine-learning/implement-on-behalf-of-token-exchange-for-multi-tenant-agents-with-amazon-bedrock-agentcore-gateway/)
- [Protocol-based tools (MCP) — prescriptive guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/agentic-ai-frameworks/protocol-based-tools.html)
