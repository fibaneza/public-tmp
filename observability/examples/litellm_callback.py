"""LiteLLM callback: capture cost, tokens, model and timings for every call.

LiteLLM fires callbacks around each completion. The handler receives the request
kwargs, the response, and start/end timestamps — which is where cost attribution
has to happen, because it is the only place that sees both the metadata FastAPI
attached and the usage the provider returned.

Register once at startup:

    import litellm
    litellm.callbacks = [LLMTelemetryHandler(environment="prod")]

Then every call carries identity:

    await litellm.acompletion(
        model="claude-opus-5",
        messages=[...],
        metadata={"user_id": ..., "session_id": ..., "request_id": ...},
    )

------------------------------------------------------------------------------
TWO TIMINGS THAT ARE NOT THE SAME NUMBER
------------------------------------------------------------------------------
  model_latency  end_time - start_time from this callback — provider time only
  total_latency  measured in FastAPI middleware — includes auth, retrieval,
                 session load, serialisation, and any retry LiteLLM performed

Reporting only the first hides your own overhead; reporting only the second
means a provider slowdown and a regression in your code look identical. Emit
both. On streaming, `ttft` matters more than either.

------------------------------------------------------------------------------
CACHE TOKENS ARE NOT INPUT TOKENS
------------------------------------------------------------------------------
Cached input is billed at roughly a tenth of the input rate, and cache *writes*
at a premium. Folding them into one `input_tokens` figure makes per-user cost
wrong in both directions. Split them out — the fields differ by provider, so
`_usage()` below normalises them.
"""

from __future__ import annotations

import logging
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

from emf import emit_llm_call

log = logging.getLogger("llm.telemetry")


def _metadata(kwargs: dict) -> dict:
    """Pull the metadata FastAPI attached.

    LiteLLM nests it under litellm_params, and the proxy adds its own keys
    alongside yours, so read defensively rather than assuming a shape.
    """
    params = kwargs.get("litellm_params") or {}
    meta = params.get("metadata") or {}
    # The proxy populates these when a virtual key is used; prefer our own.
    return {
        "user_id": meta.get("user_id") or meta.get("user_api_key_user_id") or "anonymous",
        "session_id": meta.get("session_id", ""),
        "request_id": meta.get("request_id", ""),
        "trace_id": meta.get("trace_id", ""),
        "tenant_id": meta.get("tenant_id", "default"),
        "route": meta.get("route", ""),
    }


def _usage(response_obj: Any) -> dict[str, int]:
    """Normalise token usage across providers.

    LiteLLM maps everything onto the OpenAI shape, but cache accounting is newer
    and appears in different places depending on provider and version — hence
    the fallback chain rather than one attribute access.
    """
    usage = getattr(response_obj, "usage", None)
    if usage is None:
        return dict.fromkeys(("input", "output", "cache_read", "cache_write"), 0)

    def g(obj: Any, *names: str, default: int = 0) -> int:
        for name in names:
            value = getattr(obj, name, None)
            if value is None and isinstance(obj, dict):
                value = obj.get(name)
            if value is not None:
                return int(value)
        return default

    prompt_details = getattr(usage, "prompt_tokens_details", None)

    cache_read = g(usage, "cache_read_input_tokens")
    if not cache_read and prompt_details is not None:
        cache_read = g(prompt_details, "cached_tokens")

    return {
        "input": g(usage, "prompt_tokens", "input_tokens"),
        "output": g(usage, "completion_tokens", "output_tokens"),
        "cache_read": cache_read,
        "cache_write": g(usage, "cache_creation_input_tokens"),
    }


def _cost(kwargs: dict, response_obj: Any) -> float:
    """Cost in USD for this call.

    LiteLLM computes it from its own pricing map and puts it on the callback
    kwargs. Fall back to computing it from the response, then to 0.0 — a missing
    price entry for a brand-new model must not take down the request path.
    """
    cost = kwargs.get("response_cost")
    if cost is not None:
        return float(cost)
    try:
        import litellm

        return float(litellm.completion_cost(completion_response=response_obj) or 0.0)
    except Exception:                          # unknown model, pricing map gap
        log.warning("no cost available for model=%s", kwargs.get("model"))
        return 0.0


class LLMTelemetryHandler(CustomLogger):
    """Emits one EMF record per call. Never raises into the request path."""

    def __init__(self, environment: str = "dev"):
        super().__init__()
        self.environment = environment

    # -- success ---------------------------------------------------------
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._record(kwargs, response_obj, start_time, end_time, status="success")

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._record(kwargs, response_obj, start_time, end_time, status="success")

    # -- failure ---------------------------------------------------------
    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._record(kwargs, response_obj, start_time, end_time, status="error")

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._record(kwargs, response_obj, start_time, end_time, status="error")

    # -- shared ----------------------------------------------------------
    def _record(self, kwargs, response_obj, start_time, end_time, *, status: str) -> None:
        try:
            meta = _metadata(kwargs)
            usage = _usage(response_obj)
            model = kwargs.get("model") or "unknown"

            model_latency_ms = max((end_time - start_time).total_seconds() * 1000.0, 0.0)

            # LiteLLM records this when streaming; it is the number users feel.
            ttft = kwargs.get("completion_start_time")
            ttft_ms = (
                max((ttft - start_time).total_seconds() * 1000.0, 0.0)
                if ttft is not None else None
            )

            emit_llm_call(
                model=model,
                route=meta["route"] or "unknown",
                status=status,
                environment=self.environment,
                input_tokens=usage["input"],
                output_tokens=usage["output"],
                cache_read_tokens=usage["cache_read"],
                cache_write_tokens=usage["cache_write"],
                cost_usd=_cost(kwargs, response_obj),
                model_latency_ms=model_latency_ms,
                total_latency_ms=float(meta.get("total_latency_ms") or 0.0),
                ttft_ms=ttft_ms,
                properties={
                    **{k: v for k, v in meta.items() if k != "route"},
                    "provider": (kwargs.get("litellm_params") or {}).get("custom_llm_provider", ""),
                    "stream": bool(kwargs.get("stream")),
                    "num_retries": (kwargs.get("litellm_params") or {}).get("num_retries") or 0,
                    "error": str(response_obj)[:500] if status == "error" else None,
                },
            )
        except Exception:
            # Telemetry must never be the reason a user's request fails.
            log.exception("telemetry emit failed; continuing")
