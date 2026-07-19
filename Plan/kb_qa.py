"""Standalone RetrieveAndGenerate — pure doc Q&A, no agent orchestration.

When your use case is *only* "answer questions from these documents", you
don't need a Strands agent at all: RetrieveAndGenerate is already a complete
RAG pipeline. This is the leanest, cheapest path — one API call, cited answer.

Reach for the agent (``app/runtime.py``) instead when you need tool use,
multi-step reasoning, memory, or to mix KB answers with business actions.

Multi-turn note: the KB keeps its own conversational context via the
``sessionId`` Bedrock returns. Reuse it across turns to let follow-ups
("what about the enterprise tier?") resolve against earlier ones.

Usage::

    export AWS_REGION=us-west-2
    export STRANDS_KNOWLEDGE_BASE_ID=kb-xxxx
    export KB_MODEL_ARN=arn:aws:bedrock:us-west-2:<acct>:inference-profile/us.anthropic.claude-sonnet-4-20250514-v1:0
    python -m scripts.kb_qa "What is our refund policy for enterprise plans?"
"""

from __future__ import annotations

import sys

from app.kb import retrieve_and_generate


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print('Usage: python -m scripts.kb_qa "your question"')
        return 2

    question = argv[1]
    result = retrieve_and_generate(question)

    print(result.formatted())
    # The KB session id enables multi-turn follow-ups against the same context:
    #   follow_up = retrieve_and_generate("and for monthly plans?",
    #                                     session_id=result.session_id)
    if result.session_id:
        print(f"\n[kb session: {result.session_id}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
