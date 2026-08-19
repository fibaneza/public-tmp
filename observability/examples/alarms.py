"""CloudWatch alarms and Logs Insights queries — the analysis half.

Two distinct mechanisms, and the split matters:

  ALARMS run on METRICS, which come from EMF *dimensions*. Low cardinality,
  evaluated continuously, can page someone. Use for: error rate, latency, total
  spend, throttling.

  QUERIES run on LOG RECORDS, which carry the *properties*. Unlimited
  cardinality, run on demand, cost per GB scanned. Use for: per-user cost,
  per-session forensics, "who spent the most yesterday".

Per-user budget enforcement needs both: a scheduled query to compute spend, and
an alarm on a metric the query's Lambda publishes.

    python alarms.py create     # provision alarms
    python alarms.py queries    # print the Logs Insights queries
"""

from __future__ import annotations

import os
import sys

import boto3

NAMESPACE = "LLMPlatform"
ENVIRONMENT = os.environ.get("ENVIRONMENT", "prod")
LOG_GROUP = os.environ.get("LOG_GROUP", "/ecs/llm-gateway")
SNS_TOPIC = os.environ.get("ALARM_TOPIC_ARN", "")

cw = boto3.client("cloudwatch")


def _alarm(**kwargs) -> None:
    kwargs.setdefault("ActionsEnabled", bool(SNS_TOPIC))
    if SNS_TOPIC:
        kwargs.setdefault("AlarmActions", [SNS_TOPIC])
        kwargs.setdefault("OKActions", [SNS_TOPIC])
    cw.put_metric_alarm(**kwargs)
    print(f"  ✓ {kwargs['AlarmName']}")


def create_alarms() -> None:
    env = {"Name": "Environment", "Value": ENVIRONMENT}

    # -- spend ----------------------------------------------------------
    # Hourly spend ceiling. CostMicroUSD is emitted per call; Sum over an hour
    # is total spend. 50 USD/hour = 50e6 micro-dollars.
    _alarm(
        AlarmName=f"llm-{ENVIRONMENT}-hourly-spend",
        AlarmDescription="Total LLM spend for the last hour exceeded budget",
        Namespace=NAMESPACE, MetricName="CostMicroUSD",
        Dimensions=[env, {"Name": "Model", "Value": "claude-opus-5"}],
        Statistic="Sum", Period=3600, EvaluationPeriods=1,
        Threshold=50_000_000, ComparisonOperator="GreaterThanThreshold",
        TreatMissingData="notBreaching",
    )

    # Anomaly detection catches a step change at any absolute level, which a
    # fixed threshold cannot — useful when normal spend grows over time.
    cw.put_metric_alarm(
        AlarmName=f"llm-{ENVIRONMENT}-spend-anomaly",
        AlarmDescription="LLM spend outside its expected band",
        Metrics=[
            {"Id": "m1", "MetricStat": {
                "Metric": {"Namespace": NAMESPACE, "MetricName": "CostMicroUSD",
                           "Dimensions": [env]},
                "Period": 300, "Stat": "Sum"}, "ReturnData": True},
            {"Id": "ad1", "Expression": "ANOMALY_DETECTION_BAND(m1, 3)",
             "Label": "expected band", "ReturnData": True},
        ],
        ThresholdMetricId="ad1",
        ComparisonOperator="GreaterThanUpperThreshold",
        EvaluationPeriods=2, DatapointsToAlarm=2,
        TreatMissingData="notBreaching",
        ActionsEnabled=bool(SNS_TOPIC),
        AlarmActions=[SNS_TOPIC] if SNS_TOPIC else [],
    )
    print(f"  ✓ llm-{ENVIRONMENT}-spend-anomaly")

    # -- latency --------------------------------------------------------
    _alarm(
        AlarmName=f"llm-{ENVIRONMENT}-p95-latency",
        AlarmDescription="p95 end-to-end latency above budget",
        Namespace=NAMESPACE, MetricName="TotalLatency", Dimensions=[env],
        ExtendedStatistic="p95",       # NOT Statistic — percentiles use this field
        Period=300, EvaluationPeriods=3, DatapointsToAlarm=2,
        Threshold=5000, ComparisonOperator="GreaterThanThreshold",
        TreatMissingData="notBreaching",
    )
    _alarm(
        AlarmName=f"llm-{ENVIRONMENT}-p95-ttft",
        AlarmDescription="p95 time-to-first-token above budget",
        Namespace=NAMESPACE, MetricName="TimeToFirstToken", Dimensions=[env],
        ExtendedStatistic="p95", Period=300, EvaluationPeriods=3,
        Threshold=1500, ComparisonOperator="GreaterThanThreshold",
        TreatMissingData="notBreaching",
    )

    # -- errors ---------------------------------------------------------
    # A ratio, not a count: 50 errors is fine at 100k calls and catastrophic at
    # 200. Metric math expresses that; a raw count alarm cannot.
    cw.put_metric_alarm(
        AlarmName=f"llm-{ENVIRONMENT}-error-rate",
        AlarmDescription="LLM call error rate above 5%",
        Metrics=[
            {"Id": "errors", "MetricStat": {
                "Metric": {"Namespace": NAMESPACE, "MetricName": "Calls",
                           "Dimensions": [env, {"Name": "Status", "Value": "error"}]},
                "Period": 300, "Stat": "Sum"}, "ReturnData": False},
            {"Id": "total", "MetricStat": {
                "Metric": {"Namespace": NAMESPACE, "MetricName": "Calls",
                           "Dimensions": [env]},
                "Period": 300, "Stat": "Sum"}, "ReturnData": False},
            {"Id": "rate", "Expression": "IF(total > 20, 100 * errors / total, 0)",
             "Label": "error rate %", "ReturnData": True},
        ],
        Threshold=5, ComparisonOperator="GreaterThanThreshold",
        EvaluationPeriods=2, TreatMissingData="notBreaching",
        ActionsEnabled=bool(SNS_TOPIC),
        AlarmActions=[SNS_TOPIC] if SNS_TOPIC else [],
    )
    print(f"  ✓ llm-{ENVIRONMENT}-error-rate")

    # -- composite ------------------------------------------------------
    # Pages only when errors AND latency are both bad, which suppresses the
    # single-alarm noise that trains people to ignore the pager.
    cw.put_composite_alarm(
        AlarmName=f"llm-{ENVIRONMENT}-degraded",
        AlarmDescription="LLM path degraded: errors and latency together",
        AlarmRule=(f'ALARM("llm-{ENVIRONMENT}-error-rate") '
                   f'AND ALARM("llm-{ENVIRONMENT}-p95-latency")'),
        ActionsEnabled=bool(SNS_TOPIC),
        AlarmActions=[SNS_TOPIC] if SNS_TOPIC else [],
    )
    print(f"  ✓ llm-{ENVIRONMENT}-degraded (composite)")


# ---------------------------------------------------------------------------
# Logs Insights — where high-cardinality analysis lives
# ---------------------------------------------------------------------------

QUERIES: dict[str, str] = {
    "cost_per_user": """
fields user_id, CostMicroUSD / 1000000 as cost_usd, InputTokens, OutputTokens
| filter ispresent(CostMicroUSD)
| stats sum(cost_usd) as total_usd,
        sum(InputTokens) as tokens_in,
        sum(OutputTokens) as tokens_out,
        count(*) as calls
  by user_id
| sort total_usd desc
| limit 50
""",
    "cost_per_user_per_model": """
fields user_id, Model, CostMicroUSD / 1000000 as cost_usd
| filter ispresent(CostMicroUSD)
| stats sum(cost_usd) as total_usd, count(*) as calls by user_id, Model
| sort total_usd desc
""",
    "cache_savings": """
fields CacheReadTokens, InputTokens
| filter ispresent(CacheReadTokens)
| stats sum(CacheReadTokens) as cached,
        sum(InputTokens) as uncached,
        100.0 * sum(CacheReadTokens) / (sum(CacheReadTokens) + sum(InputTokens)) as cache_hit_pct
""",
    "session_forensics": """
fields @timestamp, event, request_id, Model, CostMicroUSD / 1000000 as cost_usd,
       ModelLatency, TotalLatency
| filter session_id = 'ses_REPLACE_ME'
| sort @timestamp asc
""",
    "trace": """
fields @timestamp, @message
| filter request_id = 'REPLACE_ME'
| sort @timestamp asc
""",
    "slowest_users": """
fields user_id, TotalLatency
| filter ispresent(TotalLatency)
| stats pct(TotalLatency, 95) as p95_ms, count(*) as calls by user_id
| filter calls > 10
| sort p95_ms desc
| limit 25
""",
    "model_overhead": """
fields TotalLatency - ModelLatency as our_overhead_ms, Model
| filter ispresent(ModelLatency) and ispresent(TotalLatency)
| stats avg(our_overhead_ms) as avg_overhead,
        pct(our_overhead_ms, 95) as p95_overhead by Model
""",
    "errors_by_user": """
fields user_id, Model, error
| filter Status = 'error'
| stats count(*) as failures by user_id, Model, error
| sort failures desc
""",
}


def run_query(name: str, hours: int = 24) -> list:
    """Run one query and return its rows. This is what a scheduled per-user
    budget Lambda calls, publishing the result back as a metric so an alarm can
    watch it — queries themselves cannot trigger alarms."""
    import time

    logs = boto3.client("logs")
    start = int(time.time()) - hours * 3600
    qid = logs.start_query(logGroupName=LOG_GROUP, startTime=start,
                           endTime=int(time.time()),
                           queryString=QUERIES[name])["queryId"]
    while True:
        result = logs.get_query_results(queryId=qid)
        if result["status"] in ("Complete", "Failed", "Cancelled"):
            return result["results"]
        time.sleep(1)


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "queries"
    if command == "create":
        print(f"creating alarms for environment={ENVIRONMENT}")
        create_alarms()
    else:
        for name, query in QUERIES.items():
            print(f"\n{'=' * 70}\n-- {name}\n{'=' * 70}{query}")
