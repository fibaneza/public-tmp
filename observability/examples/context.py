"""Request-scoped correlation context.

Every log line, metric and LLM call in a request must carry the same identity
chain, or none of the per-user analysis in the accompanying document is
possible. `contextvars` is the right mechanism under FastAPI: it is
async-native and each request gets its own copy, so a concurrent request cannot
read another's user id — which a module-level global absolutely would.

    request_id  one HTTP request        — regenerated every call
    session_id  one conversation        — created by FastAPI, reused across turns
    user_id     the authenticated user  — from the token, NEVER from the body
    trace_id    one distributed trace   — spans FastAPI → LiteLLM proxy → provider
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field

_ctx: ContextVar["RequestContext | None"] = ContextVar("request_context", default=None)


@dataclass
class RequestContext:
    request_id: str
    trace_id: str
    user_id: str
    session_id: str
    tenant_id: str = "default"
    route: str = ""
    # Anything set here lands on every log line for the request. Keep it small:
    # this is copied into each LLM call's metadata and into every EMF record.
    extra: dict = field(default_factory=dict)

    def as_log_fields(self) -> dict:
        d = asdict(self)
        return {**d.pop("extra", {}), **d}


def new_ids() -> tuple[str, str]:
    """A fresh request id and trace id.

    Prefer ULIDs or the incoming `traceparent` in production — sortable ids make
    CloudWatch Logs Insights range queries far cheaper than random UUIDs.
    """
    return uuid.uuid4().hex, uuid.uuid4().hex


def set_context(ctx: RequestContext) -> None:
    _ctx.set(ctx)


def get_context() -> RequestContext | None:
    return _ctx.get()


def require_context() -> RequestContext:
    ctx = _ctx.get()
    if ctx is None:
        # Loud rather than silent: an LLM call with no context is a call whose
        # cost cannot be attributed to anyone, which is the failure this whole
        # module exists to prevent.
        raise RuntimeError("no RequestContext — is CorrelationMiddleware installed?")
    return ctx
