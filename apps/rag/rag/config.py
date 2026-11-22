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
    # Other gateway models a request may ask for instead (/ask "model"), e.g.
    # LoRA adapters served by vLLM, to compare them on the same questions.
    # A list, so a client can't pick any model the gateway routes to
    # (claude-fallback bills per token).
    allowed_models: tuple[str, ...] = tuple(
        m.strip() for m in os.environ.get("ALLOWED_MODELS", "").split(",") if m.strip())
    # Seconds. The local CPU mock can take over a minute for a RAG-sized
    # prompt on a busy laptop.
    llm_timeout: float = float(os.environ.get("LLM_TIMEOUT", "120"))
    # Chunks embedded per ONNX call. Memory grows with it: with bge-small, peak
    # 834 MiB at 32 (OOMKilled under the Job's then 768 MiB limit on EKS),
    # 452 MiB at 8; ~1 GiB at 8 with embeddinggemma.
    embed_batch: int = int(os.environ.get("EMBED_BATCH", "8"))
    chunk_size: int = int(os.environ.get("CHUNK_SIZE", "1000"))
    chunk_overlap: int = int(os.environ.get("CHUNK_OVERLAP", "150"))

    def resolve_model(self, requested: str | None) -> str:
        """The model to call: the default, or an allowed one by name."""
        if not requested or requested == self.chat_model:
            return self.chat_model
        if requested not in self.allowed_models:
            raise ValueError(f"model {requested!r} not allowed (ALLOWED_MODELS)")
        return requested


settings = Settings()
