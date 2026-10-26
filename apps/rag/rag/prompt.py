"""Builds the chat request sent to the model through LiteLLM.

Everything goes in a single user message, with no system role: the
Mistral-7B-Instruct chat template served by vLLM only accepts alternating
user/assistant turns and rejected the system message with a 400 (the local
Ollama mock accepted it, so it only showed on EKS).
"""
import re

from .chunking import Chunk

INSTRUCTIONS = (
    "Answer the question about the llm-platform-on-eks project using only the "
    "numbered context below. Cite the passages you use as [1], [2], etc. If the "
    "context doesn't contain the answer, say you don't know instead of guessing."
)

# Mistral answers in the context's language (English), not the question's:
# 0/7 Spanish questions answered in Spanish with no instruction, and still
# 0/7 with "Answer in the same language as the question" in English
# (eval/answers/2026-10-26-mistral*). So the instruction is written in the
# question's language, last, right before the model starts writing.
LANGUAGE_REMINDER = {"es": "Responde en español."}

_SPANISH_CHARS = re.compile(r"[¿¡ñáéíóú]", re.IGNORECASE)
_SPANISH_WORDS = {"qué", "que", "cómo", "como", "cuánto", "cuánta", "cuál", "cuáles", "por",
                  "para", "los", "las", "del", "una", "es", "son", "está", "hay", "hace"}


def question_language(question: str) -> str:
    """'es' or 'en'. A heuristic for the two languages the evaluation set
    covers, not general language detection."""
    if _SPANISH_CHARS.search(question):
        return "es"
    words = re.findall(r"\w+", question.lower())
    return "es" if sum(w in _SPANISH_WORDS for w in words) >= 2 else "en"


def build_messages(question: str, chunks: list[Chunk]) -> list[dict]:
    context = "\n\n".join(
        f"[{n}] {c.source} - {c.heading}\n{c.text}" for n, c in enumerate(chunks, start=1)
    )
    content = f"{INSTRUCTIONS}\n\nContext:\n{context}\n\nQuestion: {question}"
    reminder = LANGUAGE_REMINDER.get(question_language(question))
    if reminder:
        content += f"\n\n{reminder}"
    return [{"role": "user", "content": content}]
