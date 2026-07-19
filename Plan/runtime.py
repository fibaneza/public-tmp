"""AgentCore Runtime entrypoint.

The Runtime hosts this container and exposes an ``/invocations`` endpoint.
Each call arrives as ``(payload, context)``:

* ``payload`` — the JSON body sent by the caller (frontend / API).
* ``context`` — runtime-supplied metadata. Critically, ``context.session_id``
  is injected by the Runtime and used to isolate one conversation's memory
  from another. Locally it may be absent, so we fall back to a default.

Run locally with ``python -m app.runtime`` (serves the same contract on
localhost); deploy with ``agentcore launch``.
"""

from __future__ import annotations

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from .agent import build_agent
from .config import settings
from .session_meta import record_turn

app = BedrockAgentCoreApp()


def _resolve_actor_id(payload: dict, context) -> str:
    """Determine *who* is talking.

    Preference order:
      1. An authenticated identity surfaced by the runtime (custom header).
      2. An explicit ``actor_id`` in the payload (trust only if your inbound
         auth has already validated the caller).
      3. A configured default, for local development.

    Never trust a client-supplied actor id in production without validating
    it against the authenticated principal first — it scopes memory access.
    """
    headers = getattr(context, "headers", {}) or {}
    header_actor = headers.get("X-Amzn-Bedrock-AgentCore-Runtime-Custom-Actor-Id")
    return header_actor or payload.get("actor_id") or settings.default_actor_id


@app.entrypoint
def invoke(payload: dict, context) -> dict:
    """Handle one agent invocation and return the reply."""
    prompt = payload.get("prompt")
    if not prompt:
        return {"error": "Missing 'prompt' in payload."}

    actor_id = _resolve_actor_id(payload, context)
    session_id = getattr(context, "session_id", None) or "local-session"

    agent = build_agent(actor_id=actor_id, session_id=session_id)
    result = agent(prompt)

    # Best-effort session-metadata upsert to Aurora (agent_meta schema).
    # Metadata only — never content. Failures are logged, never raised.
    record_turn(session_id=session_id, actor_id=actor_id)

    # `result` is a Strands result object; str() yields the assistant's text.
    return {
        "response": str(result),
        "actor_id": actor_id,
        "session_id": session_id,
    }


if __name__ == "__main__":
    # Serves the same /invocations contract locally for development.
    app.run()
