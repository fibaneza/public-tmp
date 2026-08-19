"""FastAPI app: correlation middleware, session handling, and instrumented calls.

Run locally:
    uvicorn app:app --host 0.0.0.0 --port 8080

The three things this file demonstrates:

  1. Middleware that establishes the identity chain once, so nothing downstream
     has to thread ids through call signatures.
  2. Passing that chain to LiteLLM as `metadata`, which is what makes the
     callback in litellm_callback.py able to attribute cost to a user.
  3. Measuring end-to-end latency here — LiteLLM cannot see it — and TTFT on the
     streaming path, which is the number users actually perceive.
"""

from __future__ import annotations

import json
import logging
import os
import time

import litellm
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from context import RequestContext, get_context, new_ids, require_context, set_context
from emf import emit
from litellm_callback import LLMTelemetryHandler
from sessions import SessionStore

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
DEFAULT_MODEL = os.environ.get("LLM_MODEL", "claude-opus-5")

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("app")

litellm.callbacks = [LLMTelemetryHandler(environment=ENVIRONMENT)]
litellm.drop_params = True          # tolerate provider-specific params when routing

app = FastAPI(title="LLM Gateway")
sessions = SessionStore()


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

def jlog(event: str, **fields) -> None:
    """One JSON object per line, always carrying the correlation context.

    Plain-text logs cannot be joined to metrics. Every line here is queryable in
    Logs Insights by user_id, session_id or request_id — which is what makes the
    debugging workflow in the accompanying document possible.
    """
    ctx = get_context()
    print(json.dumps({
        "event": event,
        "level": fields.pop("level", "INFO"),
        "ts": time.time(),
        **(ctx.as_log_fields() if ctx else {}),
        **fields,
    }, separators=(",", ":"), default=str), flush=True)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def current_user(authorization: str = Header(default="")) -> dict:
    """Resolve the caller from the token.

    Deliberately NOT from the request body. A `user_id` a client can set is a
    suggestion, and every cost figure and ACL downstream would inherit it.
    Replace the stub with real JWT verification.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    # claims = jwt.decode(token, key, algorithms=["RS256"], audience=...)
    return {"user_id": f"u_{token[:12]}", "tenant_id": "acme"}


# ---------------------------------------------------------------------------
# Correlation middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    request_id, trace_id = new_ids()
    # Honour an upstream trace id (ALB, API Gateway, or a caller) so one trace
    # spans the whole system rather than restarting at this service.
    incoming = request.headers.get("x-amzn-trace-id") or request.headers.get("traceparent")
    if incoming:
        trace_id = incoming

    set_context(RequestContext(
        request_id=request_id, trace_id=trace_id,
        user_id="anonymous", session_id="",
        route=request.url.path,
    ))

    started = time.perf_counter()
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        status = 500
        jlog("request.failed", level="ERROR")
        raise
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        ctx = get_context()
        jlog("request.completed", status=status, duration_ms=round(elapsed_ms, 1))
        # HTTP-level metric. Note user_id is a property, never a dimension.
        emit(
            metrics={"RequestLatency": (elapsed_ms, "Milliseconds"),
                     "Requests": (1, "Count")},
            dimensions={"Environment": ENVIRONMENT,
                        "Route": request.url.path,
                        "Status": "success" if status < 400 else "error"},
            properties={"user_id": ctx.user_id if ctx else "anonymous",
                        "request_id": request_id, "trace_id": trace_id,
                        "http_status": status},
            dimension_sets=[["Environment", "Route"], ["Environment", "Status"]],
        )

    response.headers["x-request-id"] = request_id
    return response


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None      # returned by a previous call
    model: str | None = None


@app.post("/chat")
async def chat(body: ChatRequest, user: dict = Depends(current_user)):
    ctx = require_context()
    ctx.user_id = user["user_id"]
    ctx.tenant_id = user["tenant_id"]

    session = sessions.get_or_create(body.session_id, ctx.user_id, ctx.tenant_id)
    ctx.session_id = session.session_id
    set_context(ctx)

    started = time.perf_counter()
    jlog("chat.started", turn=session.turn_count, model=body.model or DEFAULT_MODEL)

    response = await litellm.acompletion(
        model=body.model or DEFAULT_MODEL,
        messages=session.messages + [{"role": "user", "content": body.message}],
        # THE line that makes per-user cost attribution work. Everything the
        # callback needs travels with the call.
        metadata={
            "user_id": ctx.user_id,
            "session_id": ctx.session_id,
            "request_id": ctx.request_id,
            "trace_id": ctx.trace_id,
            "tenant_id": ctx.tenant_id,
            "route": ctx.route,
        },
    )

    text = response.choices[0].message.content
    cost = float(getattr(response, "_hidden_params", {}).get("response_cost") or 0.0)
    tokens = int(getattr(response.usage, "total_tokens", 0) or 0)

    sessions.append_turn(session, body.message, text, cost_usd=cost, tokens=tokens)
    jlog("chat.completed",
         duration_ms=round((time.perf_counter() - started) * 1000, 1),
         cost_usd=cost, tokens=tokens, session_cost_usd=session.cost_usd)

    return {"reply": text, "session_id": session.session_id,
            "request_id": ctx.request_id, "usage": {"tokens": tokens, "cost_usd": cost}}


@app.post("/chat/stream")
async def chat_stream(body: ChatRequest, user: dict = Depends(current_user)):
    """Streaming path — the only one where TTFT is measurable.

    LiteLLM records its own first-token timestamp for the callback; this
    measures it from the *client's* side of the app, which includes your
    serialisation and any buffering in between. The two differing is itself
    a useful signal.
    """
    ctx = require_context()
    ctx.user_id = user["user_id"]
    session = sessions.get_or_create(body.session_id, ctx.user_id, user["tenant_id"])
    ctx.session_id = session.session_id
    set_context(ctx)

    async def generate():
        started = time.perf_counter()
        ttft_ms: float | None = None
        chunks: list[str] = []

        stream = await litellm.acompletion(
            model=body.model or DEFAULT_MODEL,
            messages=session.messages + [{"role": "user", "content": body.message}],
            stream=True,
            # Without this, streamed calls report zero tokens and zero cost —
            # a silent hole in per-user accounting that is easy to miss.
            stream_options={"include_usage": True},
            metadata={"user_id": ctx.user_id, "session_id": ctx.session_id,
                      "request_id": ctx.request_id, "trace_id": ctx.trace_id,
                      "tenant_id": ctx.tenant_id, "route": ctx.route},
        )

        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if not delta:
                continue
            if ttft_ms is None:
                ttft_ms = (time.perf_counter() - started) * 1000.0
                jlog("chat.first_token", ttft_ms=round(ttft_ms, 1))
            chunks.append(delta)
            yield f"data: {json.dumps({'delta': delta})}\n\n"

        sessions.append_turn(session, body.message, "".join(chunks))
        yield f"data: {json.dumps({'done': True, 'session_id': session.session_id})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/healthz")
async def healthz():
    """Kept free of LLM calls — an ECS health check that costs money per probe
    is a real and surprisingly common way to burn budget."""
    return {"ok": True, "environment": ENVIRONMENT}
