from rag.chunking import Chunk
from rag.prompt import build_messages


def test_context_is_numbered_for_citations():
    chunks = [Chunk("README.md", "Results", "cold start 5:14"), Chunk("docs/x.md", "Costs", "$3.57")]
    system, user = build_messages("How fast is the cold start?", chunks)
    assert system["role"] == "system"
    assert "[1] README.md - Results\ncold start 5:14" in system["content"]
    assert "[2] docs/x.md - Costs\n$3.57" in system["content"]
    assert user == {"role": "user", "content": "How fast is the cold start?"}


def test_model_is_told_not_to_guess():
    system, _ = build_messages("q", [])
    assert "don't know" in system["content"]
