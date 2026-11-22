import pytest

from rag.config import Settings


def test_resolve_model_defaults_to_chat_model():
    s = Settings(chat_model="local-mistral", allowed_models=("rag-behavior-v1",))
    assert s.resolve_model(None) == "local-mistral"
    assert s.resolve_model("") == "local-mistral"
    assert s.resolve_model("local-mistral") == "local-mistral"


def test_resolve_model_allows_listed_models_only():
    s = Settings(chat_model="local-mistral", allowed_models=("rag-behavior-v1",))
    assert s.resolve_model("rag-behavior-v1") == "rag-behavior-v1"
    with pytest.raises(ValueError, match="not allowed"):
        s.resolve_model("claude-fallback")


def test_no_allowed_models_by_default():
    assert Settings(chat_model="local-mistral", allowed_models=()).resolve_model(None) == "local-mistral"
    with pytest.raises(ValueError):
        Settings(chat_model="local-mistral", allowed_models=()).resolve_model("anything")
