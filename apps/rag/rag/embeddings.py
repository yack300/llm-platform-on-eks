"""Text embeddings on CPU with fastembed (ONNX Runtime, no PyTorch).

The model is baked into the image at build time (Dockerfile) and the image
runs with HF_HUB_OFFLINE=1, so pods never download it: no network
dependency at startup and the same weights every run.
"""
import math
import os
from functools import lru_cache


def cpu_limit(path: str = "/sys/fs/cgroup/cpu.max") -> int:
    """CPUs this container may use: its cgroup quota, not the node's cores.

    ONNX Runtime otherwise starts a thread per core it can see (8 on the kind
    node) inside a 1-CPU limit, and the kernel throttles the container for
    most of every period: ingestion took over 6 minutes instead of seconds.
    """
    try:
        quota, period = open(path).read().split()
        if quota != "max":
            return max(1, math.ceil(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    return os.cpu_count() or 1


# The tokenizer (Rust, rayon) and OpenMP size their thread pools the same
# way, when first used: cap them before fastembed is imported. Measured at
# 1 CPU, 32 chunks took 34 s with one thread and 81 s with the default eight.
for _var in ("RAYON_NUM_THREADS", "OMP_NUM_THREADS"):
    os.environ.setdefault(_var, str(cpu_limit()))

from fastembed import TextEmbedding  # noqa: E402

from .config import settings  # noqa: E402


@lru_cache(maxsize=1)
def _model() -> TextEmbedding:
    return TextEmbedding(settings.embedding_model, cache_dir=settings.model_cache, threads=cpu_limit())


def embed(texts: list[str]) -> list[list[float]]:
    """Documents (chunks) to index."""
    texts = [settings.document_prefix + t for t in texts]
    return [vector.tolist() for vector in _model().embed(texts)]


def embed_query(text: str) -> list[float]:
    """A search query."""
    return next(iter(_model().embed([settings.query_prefix + text]))).tolist()


def dimension() -> int:
    return len(embed(["dimension probe"])[0])
