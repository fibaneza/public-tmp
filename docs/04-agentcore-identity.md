# AgentCore Identity

> **Scope:** How agents authenticate — inbound (who may call the agent) vs outbound (how the
> agent authenticates to downstream services), workload identity, and the token vault. Python
> decorators for outbound auth. Current as of July 2026 (GA). For end-user auth patterns
> (Cognito, Entra ID), see [`09-authentication-and-authorization.md`](09-authentication-and-authorization.md).

## Concept

Identity is a **credential broker and agent-identity directory**. Its central idea is a clean
separation:

- **Inbound auth** — who is allowed to invoke the agent.
- **Outbound auth** — how the agent authenticates to downstream services (GitHub, Google, your
  APIs, other AWS services).

It provides a **token vault** (KMS-encrypted, customer-managed-key capable) that stores OAuth
access/refresh tokens, API keys, and client secrets so they stay out of your agent code and
logs. It is powered by Amazon Cognito under the hood and is **IdP-agnostic** — reuse your
existing Cognito, Okta, Microsoft Entra ID, or Auth0 with no user migration.

The most important rule: **never forward the end user's inbound token to a downstream
service.** Identity deliberately separates ingress identity from egress credentials to prevent
credential leakage.

## Inbound auth

Two mechanisms:

- **AWS IAM (SigV4)** — for service-to-service callers and the gateway-fronts-Runtime pattern.
- **OAuth 2.0 JWT bearer tokens** — configured with a **Discovery URL**, **allowed audiences**,
  and **allowed clients**. Flow: user authenticates with the IdP → client receives a bearer
  token → passes it in `Authorization` → Runtime/Gateway validates against the discovery URL's
  public keys.

## Outbound auth — two flows

- **User-delegated (3-legged / authorization_code)** — the agent acts *on behalf of a user*
  (e.g. read that user's GitHub). Triggers a consent screen the first time; the vault then
  caches and refreshes the token.
- **Autonomous (2-legged / client_credentials)** — the agent acts *as itself* (a service
  identity), no user in the loop.

Use user-delegated when downstream actions must respect the end user's permissions; reserve
autonomous for genuinely agent-owned actions.

## Workload identity & token exchange

Each agent gets a distinct **workload identity** (a unique ARN). The control APIs:

- `GetWorkloadAccessToken` / `GetWorkloadAccessTokenForUserId` — obtain a workload token,
  optionally scoped to a specific user id.
- `GetResourceOauth2Token` — get or refresh a downstream OAuth token from the vault; returns a
  consent `authorizationUrl` when no valid token is cached yet.

## The ergonomic path — Python decorators

Don't call the token APIs by hand. The SDK injects credentials into your function:

```python
from bedrock_agentcore.identity.auth import requires_access_token, requires_api_key

@requires_access_token(
    provider_name="github-oauth",
    scopes=["repo"],
    auth_flow="USER_FEDERATION",   # 3LO on behalf of the user; use "M2M" for 2LO
    into="access_token",
)
async def list_repos(*, access_token: str):
    # access_token is fetched, vault-cached, and refreshed for you
    ...

@requires_api_key(provider_name="partner-api-key", into="api_key")
async def call_partner(*, api_key: str):
    ...
```

`auth_flow="USER_FEDERATION"` = user-delegated (3LO); `"M2M"` = autonomous (2LO). Both work in
sync and async contexts. Underneath, providers are created once with
`create_oauth2_credential_provider` / `create_api_key_credential_provider` (or via the CLI:
`agentcore add credential`).

## Do

- Reuse your existing IdP (Cognito/Okta/Entra) — no user migration.
- Let the vault store and refresh tokens; lean on refresh tokens to avoid repeated user consent.
- Use user-delegated mode when actions must respect end-user permissions; autonomous only for
  agent-owned actions.
- Rotate client secrets/API keys via Secrets Manager.

## Don't

- Don't forward the inbound user token to downstream services — exchange it via Identity.
- Don't log tokens or embed secrets in agent code/tool schemas — that is what the vault is for.
- Don't grant a single agent both a broad autonomous identity and user-delegated scopes it
  doesn't need — scope each provider to its purpose.

## Sources

- [Securing AI agents with AgentCore Identity](https://aws.amazon.com/blogs/security/securing-ai-agents-with-amazon-bedrock-agentcore-identity/)
- [Runtime — how it works (identity context)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html)
- [Secure AI agents with AgentCore Identity (token-exchange sequence)](https://aws.amazon.com/blogs/machine-learning/secure-ai-agents-with-amazon-bedrock-agentcore-identity-on-amazon-ecs/)
- [Python SDK — identity API reference](https://aws.github.io/bedrock-agentcore-starter-toolkit/api-reference/identity.html)
