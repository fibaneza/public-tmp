"""CloudWatch metrics without Grafana, without PutMetricData: Embedded Metric Format.

EMF is a JSON shape you write to *stdout*. On ECS the awslogs driver ships it to
CloudWatch Logs, and CloudWatch extracts the metrics automatically. That gives
you three things a PutMetricData call does not:

  * no API call in the request path — no added latency, no throttling, no retries
  * the metric and the full context arrive as ONE record, so a spike in a graph
    can be drilled into with Logs Insights and traced back to a request id
  * no IAM permission beyond the log driver you already have

------------------------------------------------------------------------------
THE ONE THING TO GET RIGHT: DIMENSIONS vs PROPERTIES
------------------------------------------------------------------------------
Every unique combination of dimension VALUES creates a separate CloudWatch
custom metric, billed monthly, forever. Dimensions are for low-cardinality
facets you want to alarm on. Properties are just fields on the log record —
free, unlimited cardinality, queryable with Logs Insights.

    Dimension  (bounded)     Model, Environment, Route, Status, Provider
    Property   (unbounded)   user_id, session_id, request_id, trace_id, prompt hash

Putting `user_id` in Dimensions is the single most expensive mistake available
here: 20,000 users x 4 models is 80,000 custom metrics — a five-figure monthly
bill for data you can get from a Logs Insights query over the same records.

Per-user cost still works: it is a query over the properties, not a metric.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Iterable, Literal

Unit = Literal["Count", "Milliseconds", "Seconds", "Bytes", "None"]

NAMESPACE = "LLMPlatform"

# Dimension sets: each inner list is one metric grouping CloudWatch will create.
# Keep this list short and every member low-cardinality. Three sets over
# 4 models x 3 routes x 2 statuses is ~30 metrics — about $9/month, fine.
DEFAULT_DIMENSION_SETS = [
    ["Environment", "Model"],       # per-model cost and latency
    ["Environment", "Route"],       # per-endpoint health
    ["Environment", "Status"],      # error rate
]


def emit(
    metrics: dict[str, tuple[float, Unit]],
    dimensions: dict[str, str],
    properties: dict[str, Any] | None = None,
    *,
    namespace: str = NAMESPACE,
    dimension_sets: Iterable[list[str]] | None = None,
    stream=sys.stdout,
) -> dict:
    """Write one EMF record. Returns it, mostly so tests can assert on it.

    `metrics`    name -> (value, unit)
    `dimensions` low-cardinality facets — these BECOME custom metrics
    `properties` high-cardinality context — free, queryable, never a dimension
    """
    properties = properties or {}

    overlap = set(properties) & set(dimensions)
    if overlap:
        # A field can be one or the other. Silently allowing both is how
        # user_id ends up as a dimension six months later.
        raise ValueError(f"fields declared as both dimension and property: {overlap}")

    sets = [s for s in (dimension_sets or DEFAULT_DIMENSION_SETS)
            if all(k in dimensions for k in s)]

    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),   # epoch MILLIseconds, not seconds
            "CloudWatchMetrics": [{
                "Namespace": namespace,
                "Dimensions": sets,
                "Metrics": [{"Name": name, "Unit": unit}
                            for name, (_v, unit) in metrics.items()],
            }],
        },
        **dimensions,
        **{name: value for name, (value, _u) in metrics.items()},
        **properties,
    }

    # One line, no indentation — the awslogs driver splits on newlines, so a
    # pretty-printed record becomes N broken log events and zero metrics.
    print(json.dumps(record, separators=(",", ":"), default=str), file=stream, flush=True)
    return record


def emit_llm_call(
    *,
    model: str,
    route: str,
    status: str,
    environment: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    cost_usd: float,
    model_latency_ms: float,
    total_latency_ms: float,
    ttft_ms: float | None,
    properties: dict[str, Any],
) -> dict:
    """The record every LLM call should produce.

    Cost is emitted in *micro*-dollars. CloudWatch statistics on values around
    1e-4 lose precision and graph as a flat zero line; integers of micro-dollars
    keep Sum and Average readable. Divide by 1e6 when you display it.
    """
    metrics: dict[str, tuple[float, Unit]] = {
        "InputTokens": (input_tokens, "Count"),
        "OutputTokens": (output_tokens, "Count"),
        "CacheReadTokens": (cache_read_tokens, "Count"),
        "CacheWriteTokens": (cache_write_tokens, "Count"),
        "CostMicroUSD": (round(cost_usd * 1_000_000), "Count"),
        "ModelLatency": (model_latency_ms, "Milliseconds"),
        "TotalLatency": (total_latency_ms, "Milliseconds"),
        "Calls": (1, "Count"),
    }
    if ttft_ms is not None:
        metrics["TimeToFirstToken"] = (ttft_ms, "Milliseconds")

    return emit(
        metrics=metrics,
        dimensions={
            "Environment": environment,
            "Model": model,          # bounded: the models you actually route to
            "Route": route,          # bounded: your endpoints
            "Status": status,        # bounded: success | error | timeout | refusal
        },
        properties=properties,       # user_id, session_id, request_id, trace_id...
    )
