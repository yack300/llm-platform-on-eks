from rag.chunking import Chunk
from rag.prompt import build_messages, question_language


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


def test_spanish_question_gets_a_spanish_instruction():
    (message,) = build_messages("¿Cuánto tarda el arranque en frío?", [])
    assert message["content"].endswith("Responde en español.")


def test_english_question_has_no_language_reminder():
    (message,) = build_messages("How long is a cold start?", [])
    assert message["content"].endswith("Question: How long is a cold start?")


def test_question_language():
    assert question_language("¿Qué versión de Kubernetes usa?") == "es"
    assert question_language("Por que los nodos no necesitan NAT") == "es"
    assert question_language("What is the capital of France?") == "en"
    assert question_language("Which model does vLLM serve?") == "en"
