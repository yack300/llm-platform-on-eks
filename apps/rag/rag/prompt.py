"""Builds the chat request sent to the model through LiteLLM.

Everything goes in a single user message, with no system role: the
Mistral-7B-Instruct chat template served by vLLM only accepts alternating
user/assistant turns and rejected the system message with a 400 (the local
Ollama mock accepted it, so it only showed on EKS).
"""
from .chunking import Chunk

INSTRUCTIONS = (
    "Answer the question about the llm-platform-on-eks project using only the "
    "numbered context below. Cite the passages you use as [1], [2], etc. If the "
    "context doesn't contain the answer, say you don't know instead of guessing. "
    "Write the answer in the language of the question, even though the context "
    "is in English."
)
# Without any of this, Mistral answered every Spanish question in English
# (0/7, eval/answers/2026-10-26-mistral.summary.md). The reminder goes last,
# right before the model starts writing.
REMINDER = "Answer in the same language as the question."


def build_messages(question: str, chunks: list[Chunk]) -> list[dict]:
    context = "\n\n".join(
        f"[{n}] {c.source} - {c.heading}\n{c.text}" for n, c in enumerate(chunks, start=1)
    )
    content = f"{INSTRUCTIONS}\n\nContext:\n{context}\n\nQuestion: {question}\n\n{REMINDER}"
    return [{"role": "user", "content": content}]
