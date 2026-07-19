"""Central configuration, driven entirely by environment variables.

Nothing here provisions AWS resources. IDs for the Memory store and the
Knowledge Base are created out of band (see ``scripts/provision_memory.py``
and your Bedrock console / IaC) and passed in at deploy time, e.g.::

    agentcore launch \\
        --env BEDROCK_AGENTCORE_MEMORY_ID=mem-xxxx \\
        --env STRANDS_KNOWLEDGE_BASE_ID=kb-xxxx

Keeping configuration in one module (rather than scattered ``os.getenv``
calls) means the agent fails fast at startup with a clear message if a
required variable is missing, instead of failing deep inside a request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings resolved from the environment."""

    region: str = field(default_factory=lambda: os.getenv("AWS_REGION", "us-west-2"))

    # Bedrock model used by the agent. Inference-profile IDs (the "us." prefix)
    # are required for cross-region models such as Claude Sonnet.
    model_id: str = field(
        default_factory=lambda: os.getenv(
            "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0"
        )
    )

    # Managed AgentCore Memory resource. Optional: when unset the agent runs
    # statelessly (no cross-turn memory), which is handy for local smoke tests.
    memory_id: str | None = field(
        default_factory=lambda: os.getenv("BEDROCK_AGENTCORE_MEMORY_ID")
    )

    # Bedrock Knowledge Base for RAG. The strands_tools `retrieve` tool reads
    # STRANDS_KNOWLEDGE_BASE_ID; some older examples use KNOWLEDGE_BASE_ID, so
    # we accept either and normalise below.
    knowledge_base_id: str | None = field(
        default_factory=lambda: os.getenv("STRANDS_KNOWLEDGE_BASE_ID")
        or os.getenv("KNOWLEDGE_BASE_ID")
    )

    # RAG strategy for the Knowledge Base:
    #   "retrieve"              → strands_tools.retrieve; agent model generates.
    #   "retrieve_and_generate" → Bedrock RetrieveAndGenerate; KB generates.
    kb_mode: str = field(
        default_factory=lambda: os.getenv("KB_MODE", "retrieve_and_generate")
    )

    # Model ARN for RetrieveAndGenerate. REQUIRED when using an inference
    # profile (ids prefixed us./eu./apac.), because the synthesised
    # foundation-model ARN only works for on-demand models. Example:
    #   arn:aws:bedrock:us-west-2:<acct>:inference-profile/us.anthropic...-v1:0
    kb_model_arn: str | None = field(
        default_factory=lambda: os.getenv("KB_MODEL_ARN")
    )

    # Number of chunks the KB retrieves before generating.
    kb_num_results: int = field(
        default_factory=lambda: int(os.getenv("KB_NUM_RESULTS", "5"))
    )

    # Optional Retrieve search override: "HYBRID" or "SEMANTIC". Only set
    # HYBRID on stores that support it (e.g. OpenSearch Serverless); unset
    # lets Bedrock choose.
    kb_search_type: str | None = field(
        default_factory=lambda: os.getenv("KB_SEARCH_TYPE")
    )

    # --- Session metadata registry (Aurora PostgreSQL via RDS Data API) ---
    # All three must be set to enable the per-turn metadata upsert; otherwise
    # the write is skipped (local dev, tests).
    aurora_cluster_arn: str | None = field(
        default_factory=lambda: os.getenv("AURORA_CLUSTER_ARN")
    )
    aurora_secret_arn: str | None = field(
        default_factory=lambda: os.getenv("AURORA_SECRET_ARN")
    )
    aurora_database: str | None = field(
        default_factory=lambda: os.getenv("AURORA_DATABASE")
    )

    # Fallback actor id for local runs. In production the actor id identifies
    # the end user and should come from the authenticated request, never a
    # hardcoded default.
    default_actor_id: str = field(
        default_factory=lambda: os.getenv("DEFAULT_ACTOR_ID", "local-user")
    )

    def __post_init__(self) -> None:
        # The `retrieve` tool resolves the KB from the environment, so make
        # sure both spellings are populated when we have an ID.
        if self.knowledge_base_id:
            os.environ.setdefault("STRANDS_KNOWLEDGE_BASE_ID", self.knowledge_base_id)
            os.environ.setdefault("KNOWLEDGE_BASE_ID", self.knowledge_base_id)

    @property
    def memory_enabled(self) -> bool:
        return bool(self.memory_id)

    @property
    def kb_enabled(self) -> bool:
        return bool(self.knowledge_base_id)

    @property
    def session_meta_enabled(self) -> bool:
        return bool(
            self.aurora_cluster_arn and self.aurora_secret_arn and self.aurora_database
        )


settings = Settings()
