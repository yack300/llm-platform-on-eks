"""Settings, all from environment variables (set in k8s/rag/)."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    qdrant_url: str = os.environ.get("QDRANT_URL", "http://qdrant.llm-platform.svc.cluster.local:6333")
    # An alias, not a collection: each ingestion builds a new collection and
    # repoints the alias, so queries never hit a half-built index.
    collection: str = os.environ.get("COLLECTION", "platform-docs")
    # Must match the model baked into the image (Dockerfile EMBEDDING_MODEL).
    embedding_model: str = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    model_cache: str = os.environ.get("MODEL_CACHE", "/models")
    corpus_dir: str = os.environ.get("CORPUS_DIR", "/corpus")
    litellm_url: str = os.environ.get("LITELLM_URL", "http://litellm-gateway.llm-platform.svc.cluster.local:4000/v1")
    litellm_api_key: str = os.environ.get("LITELLM_API_KEY", "")
    # Routed by LiteLLM: vLLM (or the local Ollama mock), with claude-fallback
    # while it cold-starts.
    chat_model: str = os.environ.get("CHAT_MODEL", "local-mistral")
    # Seconds. The local CPU mock can take over a minute for a RAG-sized
    # prompt on a busy laptop.
    llm_timeout: float = float(os.environ.get("LLM_TIMEOUT", "120"))
    chunk_size: int = int(os.environ.get("CHUNK_SIZE", "1000"))
    chunk_overlap: int = int(os.environ.get("CHUNK_OVERLAP", "150"))


settings = Settings()
