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
    # Picked with rag.evaluate (docs/retrieval-eval.md): multilingual, unlike
    # the previous BAAI/bge-small-en-v1.5, at ~850 MiB resident instead of ~350.
    embedding_model: str = os.environ.get("EMBEDDING_MODEL", "google/embeddinggemma-300m")
    # Task prompts prepended before embedding: embeddinggemma was trained
    # with them, and retrieval drops without (hit@4 0.87 -> 0.90 with them).
    # Set both to "" for models without task prompts.
    query_prefix: str = os.environ.get("QUERY_PREFIX", "task: search result | query: ")
    document_prefix: str = os.environ.get("DOCUMENT_PREFIX", "title: none | text: ")
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
    # Chunks embedded per ONNX call. Memory grows with it: with bge-small, peak
    # 834 MiB at 32 (OOMKilled under the Job's then 768 MiB limit on EKS),
    # 452 MiB at 8; ~1 GiB at 8 with embeddinggemma.
    embed_batch: int = int(os.environ.get("EMBED_BATCH", "8"))
    chunk_size: int = int(os.environ.get("CHUNK_SIZE", "1000"))
    chunk_overlap: int = int(os.environ.get("CHUNK_OVERLAP", "150"))


settings = Settings()
