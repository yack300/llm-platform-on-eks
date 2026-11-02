from rag.chunking import Chunk
from rag.prompt import build_messages


def test_single_user_message():
    # Mistral's chat template rejects a system role (400 from vLLM).
    messages = build_messages("q", [])
    assert [m["role"] for m in messages] == ["user"]


def test_context_is_numbered_and_question_comes_last():
    chunks = [Chunk("README.md", "Results", "cold start 5:14"), Chunk("docs/x.md", "Costs", "$3.57")]
    (message,) = build_messages("How fast is the cold start?", chunks)
    content = message["content"]
    assert "[1] README.md - Results\ncold start 5:14" in content
    assert "[2] docs/x.md - Costs\n$3.57" in content
    assert content.endswith("Question: How fast is the cold start?")


def test_model_is_told_not_to_guess():
    (message,) = build_messages("q", [])
    assert "don't know" in message["content"]
