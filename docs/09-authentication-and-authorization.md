# Authentication & Authorization

> **Scope:** End-user auth in front of Bedrock/AgentCore — Amazon Cognito, Microsoft Entra ID
> federation, OAuth2/OIDC, API Gateway authorizers, and mapping identity to session/tenant.
> Agent-facing auth (inbound/outbound, token vault) is in
> [`04-agentcore-identity.md`](04-agentcore-identity.md). Current as of July 2026.

## The pattern

**Front the app/agent with API Gateway; never ship AWS credentials to browsers or mobile.** The
backend (Lambda/ECS) holds an IAM role that calls Bedrock; Bedrock authorizes the request via
**SigV4**, which boto3 signs automatically. The edge validates the user's JWT before anything
reaches your compute. This is the shape in [`../agentcore`](../agentcore) and
[`../Plan/architecture-v2.md`](../Plan/architecture-v2.md).

Three layers, kept distinct:

1. **User identity** — your IdP (Cognito/Entra/Okta) issues a JWT.
2. **Edge authorization** — API Gateway validates the JWT and maps claims to permissions.
3. **AWS authorization** — the backend role (SigV4) authorizes the actual Bedrock/AgentCore call;
   AgentCore Identity handles agent-facing auth.

## Amazon Cognito

- **User pools** = a user directory issuing **JWTs** (ID / access / refresh).
- **Identity pools** = exchange a JWT for **temporary AWS credentials** (SigV4) when a client
  must call AWS directly.

### API Gateway authorizers

| API type | Authorizer | Config |
|---|---|---|
| REST API | `COGNITO_USER_POOLS` | Attach the user pool; API Gateway validates the token. |
| HTTP API | **JWT authorizer** | `issuer = https://cognito-idp.{region}.amazonaws.com/{userPoolId}`, `audience = appClientId`. |
| Either | **Lambda authorizer** | Custom logic: validate the JWT, then map a Cognito **group → IAM policy** (looked up in DynamoDB) for fine-grained authZ. |

Use **custom scopes** (`resourceServer/scope`) for coarse API authorization, and a Lambda
authorizer when you need per-group or per-tenant policy. The authorizer's `context` map
propagates downstream (e.g. `tenant_id`) — use it, don't re-parse the token in every handler.

## Microsoft Entra ID (Azure AD) federation

Add Entra ID to a Cognito user pool as a **SAML 2.0** or **OIDC** identity provider. Cognito
then **normalizes every IdP into one consistent JWT**, so your backend validates a single token
type regardless of whether the user came from Entra, Okta, or a native pool. Supports
IdP-initiated SSO, request signing, and encrypted assertions; federated users get the same
permissions model as native users. This is the recommended way to bring enterprise SSO to a
Bedrock/AgentCore app without your backend learning multiple token formats.

AgentCore inbound auth can also point **directly** at Entra (or any OIDC IdP) via a discovery
URL — see [`04-agentcore-identity.md`](04-agentcore-identity.md). Choose:

- **Cognito-fronted federation** when you want one normalized token and Cognito features
  (groups, triggers, hosted UI).
- **Direct OIDC to AgentCore** when the agent endpoint is the boundary and you don't need
  Cognito in the path.

## WebSockets / streaming LLM responses

API Gateway **WebSocket** APIs have no built-in Cognito authorizer. Options:

- a **Lambda authorizer** that validates the JWT (passed as a query parameter on connect), or
- **IAM auth** via identity-pool SigV4.

Do not leave a streaming endpoint unauthenticated because "it's just a socket."

## Mapping identity to session / tenant

Carry the authenticated identity all the way to the model and the data:

- Pass user/tenant context to Bedrock via `requestMetadata` on Converse and via **KB metadata
  filters** (`tenant_id`) — see [`06-rag-and-knowledge-bases.md`](06-rag-and-knowledge-bases.md).
- On AgentCore, derive the **`actorId`** from the authenticated principal, not from a
  client-supplied field — the repo's [`../Plan/runtime.py`](../Plan/runtime.py) reads a custom
  runtime identity header and validates before trusting any payload `actor_id`.
- Enforce tenant isolation with per-group IAM policies *and* the retrieval filter — defense in
  depth.

## Do

- Validate the JWT at the edge (authorizer); let boto3 do SigV4 for AWS calls.
- Federate enterprise IdPs (Entra) through Cognito for one normalized token — or point AgentCore
  inbound auth directly at the OIDC discovery URL.
- Derive `actorId`/tenant from the authenticated principal; propagate via the authorizer
  `context`.
- Authenticate WebSocket connects explicitly.

## Don't

- Don't embed long-lived AWS keys in clients — use Cognito identity pools for temporary creds.
- Don't hand raw IdP tokens to backend business logic — validate and map to claims/context.
- Don't trust a client-supplied user/tenant id; don't skip WebSocket auth.
- Don't rely on a single layer — combine edge authZ with the retrieval/tenant filter.

## Sources

- [Fine-grained authZ with Cognito, API Gateway, and IAM](https://aws.amazon.com/blogs/security/building-fine-grained-authorization-using-amazon-cognito-api-gateway-and-iam/)
- [Cognito custom scopes with API Gateway](https://repost.aws/knowledge-center/cognito-custom-scopes-api-gateway)
- [Cognito federation with Azure AD / Entra ID](https://aws.amazon.com/blogs/security/how-to-set-up-amazon-cognito-for-federated-authentication-using-azure-ad/) · [SAML federation (IdP-initiated, signing, encryption)](https://aws.amazon.com/blogs/security/how-to-set-up-saml-federation-in-amazon-cognito-using-idp-initiated-single-sign-on-request-signing-and-encrypted-assertions/) · [SAML IdP docs](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-saml-idp.html)
- [Serverless strategies for streaming LLM responses](https://aws.amazon.com/blogs/compute/serverless-strategies-for-streaming-llm-responses/)
- [Authenticating external applications (M2M)](https://aws.amazon.com/blogs/security/approaches-for-authenticating-external-applications-in-a-machine-to-machine-scenario/)
- Repo: [`../agentcore`](../agentcore) · [`../Plan/architecture-v2.md`](../Plan/architecture-v2.md) · [`../Plan/runtime.py`](../Plan/runtime.py)
