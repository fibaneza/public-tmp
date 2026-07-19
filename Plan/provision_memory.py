"""Provision the AgentCore Memory resource. RUN ONCE, not per request.

Creating a Memory store is a setup step — like a database migration — not
something the agent does on the request path. Run this once (or manage the
equivalent via IaC / the console), capture the returned ID, and pass it to
the agent as ``BEDROCK_AGENTCORE_MEMORY_ID``.

The strategies below turn raw short-term turns into queryable long-term
memory. Their ``namespaceTemplates`` MUST match the keys used in
``app/memory.py``'s ``retrieval_config`` — that is the contract that lets the
session manager find what these strategies write.

Usage::

    python -m scripts.provision_memory --name AppMemory --region us-west-2
    # → prints: BEDROCK_AGENTCORE_MEMORY_ID=mem-xxxxxxxx
"""

from __future__ import annotations

import argparse

from bedrock_agentcore.memory import MemoryClient

STRATEGIES = [
    {
        "userPreferenceMemoryStrategy": {
            "name": "PreferenceLearner",
            "namespaceTemplates": ["/preferences/{actorId}"],
        }
    },
    {
        "semanticMemoryStrategy": {
            "name": "FactExtractor",
            "namespaceTemplates": ["/facts/{actorId}"],
        }
    },
    {
        "summaryMemoryStrategy": {
            "name": "SessionSummarizer",
            "namespaceTemplates": ["/summaries/{actorId}/{sessionId}"],
        }
    },
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an AgentCore Memory resource.")
    parser.add_argument("--name", default="AppMemory", help="Memory resource name.")
    parser.add_argument("--region", default="us-west-2", help="AWS region.")
    parser.add_argument(
        "--description",
        default="Application agent memory (STM + LTM strategies).",
    )
    args = parser.parse_args()

    client = MemoryClient(region_name=args.region)

    # create_memory_and_wait blocks until the resource is ACTIVE, so the
    # returned ID is immediately usable.
    memory = client.create_memory_and_wait(
        name=args.name,
        description=args.description,
        strategies=STRATEGIES,
    )

    memory_id = memory.get("id")
    print(f"Memory created and active in {args.region}.")
    print(f"BEDROCK_AGENTCORE_MEMORY_ID={memory_id}")


if __name__ == "__main__":
    main()
