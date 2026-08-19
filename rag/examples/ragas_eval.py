"""Evaluating a RAG pipeline with RAGAS -- and why the metrics are split in two.

The central idea: a RAG answer can be wrong for two unrelated reasons, and one
aggregate score hides which. So RAGAS splits the metrics along the pipeline seam:

  RETRIEVAL (did we fetch the right things?)
    context_precision  -- of what we retrieved, how much was actually relevant?
                          Low  => too much noise; fix with reranking / higher
                          threshold / smaller k.
    context_recall     -- of what was needed, how much did we retrieve?
                          Low  => the answer is not reachable; fix with hybrid
                          search, better chunking, larger k. Needs ground truth.

  GENERATION (given what we fetched, did we answer well?)
    faithfulness       -- is every claim in the answer supported by the context?
                          Low  => hallucination; fix the prompt, the model, or
                          add citation verification.
    answer_relevancy   -- does the answer address the question asked?
                          Low  => the model rambled or answered a near-miss.

Read them as a decision table:

    recall low                       -> retrieval problem, generation is innocent
    recall high + precision low      -> reranking problem
    context fine + faithfulness low  -> generation problem, retrieval is innocent
    everything high + users unhappy  -> your test set does not look like traffic

Most metrics are LLM-judged, which means they are estimates with variance. Use
them to compare *versions of your own pipeline*, never as absolute grades, and
always eyeball a sample of the judgements before trusting a delta.

Run:  python ragas_eval.py
"""

from __future__ import annotations

from dataclasses import dataclass

from _common import CORPUS, complete, format_context
from hybrid_search import HybridRetriever


# ---------------------------------------------------------------------------
# 1. The evaluation set
# ---------------------------------------------------------------------------
# 30-50 hand-written question/ground-truth pairs beat 5000 synthetic ones. Seed
# from real user queries, and deliberately include the hard classes:
#   * multi-hop      -- needs two chunks combined
#   * negative       -- the corpus genuinely does not contain the answer
#   * lexical        -- an exact ID/acronym that dense search misses
#   * paraphrase     -- no vocabulary overlap with the source passage

@dataclass
class EvalCase:
    question: str
    ground_truth: str
    kind: str = "simple"


EVAL_SET = [
    EvalCase("How long do customers have to request a refund?",
             "Customers may request a refund within 14 days of purchase.", "lexical"),
    EvalCase("What is a PIT?",
             "A PIT is a Payments Incident Thread opened in #payments-incident during a Sev-1.",
             "lexical"),
    EvalCase("How do I undo a bad release without losing in-flight payments?",
             "Run `payctl rollback --to <release>`; it drains traffic from the new pods "
             "before promoting the previous release, so no in-flight authorisation is lost.",
             "paraphrase"),
    EvalCase("If a refund is retried twice, can the customer be paid twice?",
             "No. Write endpoints accept an Idempotency-Key retained for 24 hours, and the "
             "ledger is append-only, so a replay within the window returns the stored response.",
             "multi-hop"),
    EvalCase("What is the company's holiday allowance?",
             "The provided documents do not contain this information.", "negative"),
]

ANSWER_PROMPT = """Answer using only the context. If the context does not contain the
answer, say exactly: The provided documents do not contain this information.

Context:
{context}

Question: {question}
"""


def run_pipeline(question: str, retriever: HybridRetriever, k: int = 4) -> tuple[str, list[str]]:
    """Whatever your production pipeline is, wrap it to return (answer, contexts).
    Those two fields plus question and ground_truth are all RAGAS consumes."""
    chunks = [c for c, _ in retriever.rrf(question, k=k)]
    answer = complete(ANSWER_PROMPT.format(
        context=format_context(chunks), question=question
    ))
    return answer, [c.text for c in chunks]


# ---------------------------------------------------------------------------
# 2. RAGAS
# ---------------------------------------------------------------------------

def evaluate_with_ragas(retriever: HybridRetriever, k: int = 4):
    from datasets import Dataset
    from ragas import evaluate
    from ragas.metrics import (
        answer_relevancy,
        context_precision,
        context_recall,
        faithfulness,
    )

    rows = {"question": [], "answer": [], "contexts": [], "ground_truth": []}
    for case in EVAL_SET:
        answer, contexts = run_pipeline(case.question, retriever, k=k)
        rows["question"].append(case.question)
        rows["answer"].append(answer)
        rows["contexts"].append(contexts)     # list[str] per row -- RAGAS requires this
        rows["ground_truth"].append(case.ground_truth)

    result = evaluate(
        Dataset.from_dict(rows),
        metrics=[context_precision, context_recall, faithfulness, answer_relevancy],
    )
    return result


# ---------------------------------------------------------------------------
# 3. Deterministic retrieval metrics -- run these on every commit
# ---------------------------------------------------------------------------
# LLM-judged metrics cost money and drift between runs. Rank metrics need only a
# labelled relevant set, are free, and are stable enough to gate CI on.

def hit_rate_at_k(retrieved_ids: list[str], relevant_ids: set[str], k: int) -> float:
    """Did *any* relevant chunk make the top k? The recall ceiling for generation
    -- if this is 0.6, no reranker or prompt can push end-to-end past 0.6."""
    return 1.0 if set(retrieved_ids[:k]) & relevant_ids else 0.0


def mrr(retrieved_ids: list[str], relevant_ids: set[str]) -> float:
    """Mean Reciprocal Rank: 1/rank of the first relevant hit. Sensitive to
    exactly what reranking improves -- moving the answer from rank 8 to rank 1."""
    for rank, cid in enumerate(retrieved_ids, start=1):
        if cid in relevant_ids:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved_ids: list[str], relevance: dict[str, float], k: int) -> float:
    """Normalised Discounted Cumulative Gain: graded relevance with a positional
    discount. Use when "partially relevant" is a real category in your labels."""
    import math

    def dcg(ids: list[str]) -> float:
        return sum(relevance.get(cid, 0.0) / math.log2(i + 1)
                   for i, cid in enumerate(ids[:k], start=1))

    ideal = sorted(relevance.values(), reverse=True)[:k]
    idcg = sum(rel / math.log2(i + 1) for i, rel in enumerate(ideal, start=1))
    return dcg(retrieved_ids) / idcg if idcg else 0.0


if __name__ == "__main__":
    retriever = HybridRetriever(CORPUS)

    # Deterministic pass -- no LLM, no API key needed.
    labels = {"How long do customers have to request a refund?": {"c3"},
              "What is a PIT?": {"c2"},
              "How do I undo a bad release without losing in-flight payments?": {"c1"}}
    print("=== retrieval metrics (deterministic) ===")
    for question, relevant in labels.items():
        ids = [c.id for c, _ in retriever.rrf(question, k=5)]
        print(f"  hit@3={hit_rate_at_k(ids, relevant, 3):.0f}  "
              f"mrr={mrr(ids, relevant):.3f}  "
              f"ndcg@5={ndcg_at_k(ids, {c: 1.0 for c in relevant}, 5):.3f}  {question[:45]}")

    # LLM-judged pass -- needs ANTHROPIC_API_KEY / OPENAI_API_KEY for the judge.
    print("\n=== RAGAS ===")
    try:
        print(evaluate_with_ragas(retriever))
    except Exception as exc:
        print(f"  skipped: {exc}")
