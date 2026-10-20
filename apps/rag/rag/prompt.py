"""Builds the chat request sent to the model through LiteLLM."""
from .chunking import Chunk

SYSTEM = (
    "You answer questions about the llm-platform-on-eks project using only the "
    "numbered context below. Cite the passages you use as [1], [2], etc. If the "
    "context doesn't contain the answer, say you don't know instead of guessing."
)


def build_messages(question: str, chunks: list[Chunk]) -> list[dict]:
    context = "\n\n".join(
        f"[{n}] {c.source} - {c.heading}\n{c.text}" for n, c in enumerate(chunks, start=1)
    )
    return [
        {"role": "system", "content": f"{SYSTEM}\n\nContext:\n{context}"},
        {"role": "user", "content": question},
    ]
