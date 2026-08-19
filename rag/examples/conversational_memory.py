"""Conversational memory: making "what does it do?" actually work.

Multi-turn RAG breaks in a specific, reproducible place. Turn 1 works because
the query is self-contained. Turn 2 is "what does it do?" -- and that string,
embedded on its own, is equidistant from every document you own. Retrieval
returns noise, and the model either hallucinates or apologises.

The fix is *query contextualisation*, sometimes called history-aware retrieval:
rewrite the follow-up into a standalone question using the conversation history,
retrieve with the rewrite, but answer as the original turn.

    history + "what does it do?"  ->  "What does the payctl rollback command do?"

Three things this file gets right that naive implementations miss:

  * The rewriter must be able to say "already standalone" and pass the query
    through -- rewriting an independent question damages it.
  * History must be *bounded*. Sliding window of N turns plus a rolling summary
    of what fell out, otherwise the prompt grows without limit and latency and
    cost drift up over a long session.
  * Retrieved chunks are cached per turn so a genuine follow-up about the same
    documents can skip retrieval entirely.

Run:  python conversational_memory.py
"""

from __future__ import annotations

from dataclasses import dataclass, field

from _common import CORPUS, Chunk, complete, format_context
from hybrid_search import HybridRetriever

CONTEXTUALIZE_PROMPT = """Given the conversation so far and the user's latest message,
rewrite the latest message as a standalone question that makes sense on its own.
Resolve every pronoun and ellipsis ("it", "that one", "and the other?") using the
history. Do NOT answer the question.
If the latest message is already standalone, return it unchanged.

Conversation so far:
{history}

Latest message: {question}
Standalone question:"""

ANSWER_PROMPT = """You are answering inside an ongoing conversation.
Use only the context below. Cite the bracketed source marker after each claim.
If the context does not answer it, say so.

{summary_block}Recent conversation:
{history}

Context:
{context}

User: {question}
Assistant:"""

SUMMARY_PROMPT = """Update the running summary of this conversation with the turns
below. Keep it under 120 words, keep entities and decisions, drop pleasantries.

Existing summary: {summary}
New turns:
{turns}

Updated summary:"""


@dataclass
class Turn:
    role: str
    content: str


@dataclass
class ConversationalRAG:
    retriever: HybridRetriever
    window: int = 6                       # turns kept verbatim (3 exchanges)
    turns: list[Turn] = field(default_factory=list)
    summary: str = ""                     # rolling summary of evicted turns
    last_chunks: list[Chunk] = field(default_factory=list)

    # -- history management ----------------------------------------------
    def _history_text(self) -> str:
        return "\n".join(f"{t.role}: {t.content}" for t in self.turns[-self.window:]) or "(none)"

    def _evict(self) -> None:
        """Fold anything past the window into the rolling summary instead of
        dropping it -- otherwise turn 20 loses the constraint set in turn 2."""
        if len(self.turns) <= self.window:
            return
        overflow, self.turns = self.turns[:-self.window], self.turns[-self.window:]
        turns_text = "\n".join(f"{t.role}: {t.content}" for t in overflow)
        self.summary = complete(
            SUMMARY_PROMPT.format(summary=self.summary or "(empty)", turns=turns_text)
        ).strip()

    # -- the pipeline -----------------------------------------------------
    def contextualize(self, question: str) -> str:
        if not self.turns:
            return question           # first turn is standalone by definition
        return complete(CONTEXTUALIZE_PROMPT.format(
            history=self._history_text(), question=question
        )).strip()

    def ask(self, question: str, k: int = 4) -> str:
        standalone = self.contextualize(question)
        if standalone != question:
            print(f"   [rewritten] {question!r} -> {standalone!r}")

        chunks = [c for c, _ in self.retriever.rrf(standalone, k=k)]
        self.last_chunks = chunks

        summary_block = f"Earlier in the conversation: {self.summary}\n\n" if self.summary else ""
        answer = complete(ANSWER_PROMPT.format(
            summary_block=summary_block,
            history=self._history_text(),
            context=format_context(chunks),
            question=question,          # the model sees the *original* phrasing
        ))

        self.turns += [Turn("user", question), Turn("assistant", answer)]
        self._evict()
        return answer


if __name__ == "__main__":
    session = ConversationalRAG(retriever=HybridRetriever(CORPUS))

    for turn in [
        "How do I roll back a payments deployment?",
        "What does it do to in-flight requests?",     # "it" -> the rollback command
        "And how long are the keys kept?",            # topic shift, elliptical
    ]:
        print(f"\nUSER: {turn}")
        print(f"ASSISTANT: {session.ask(turn)}")
        print(f"   [retrieved] {[c.id for c in session.last_chunks]}")
