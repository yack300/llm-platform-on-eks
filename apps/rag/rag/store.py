"""Qdrant access: versioned collections behind an alias.

Each ingestion writes a brand-new collection (`<alias>-<timestamp>`) and only
then repoints the alias at it, in one atomic call; older versions are deleted
afterwards. Queries always go through the alias, so they never see a
half-built index, and a failed ingestion leaves the previous one serving.
"""
import time
import uuid

from qdrant_client import QdrantClient, models

from .chunking import Chunk
from .config import settings

# Fixed namespace: the same chunk (source + text) always gets the same id,
# regardless of which upsert batch it lands in.
_ID_NAMESPACE = uuid.UUID("5b1c0d6e-3f0a-4c55-9a8e-6c2d6f3b9e41")


def client() -> QdrantClient:
    return QdrantClient(url=settings.qdrant_url, timeout=30)


# 5 minutes: on a fresh cluster Qdrant may still be pulling its image (the
# first two ingestion attempts on kind gave up after 60 s).
def wait_until_ready(c: QdrantClient, attempts: int = 60, delay: float = 5.0) -> None:
    for attempt in range(1, attempts + 1):
        try:
            c.get_collections()
            return
        except Exception:  # connection refused while Qdrant starts
            if attempt == attempts:
                raise
            time.sleep(delay)


def create_version(c: QdrantClient, dimension: int) -> str:
    name = f"{settings.collection}-{int(time.time())}"
    c.create_collection(name, vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE))
    return name


def upsert(c: QdrantClient, collection: str, chunks: list[Chunk], vectors: list[list[float]]) -> None:
    points = [
        models.PointStruct(
            id=str(uuid.uuid5(_ID_NAMESPACE, f"{chunk.source}\n{chunk.text}")),
            vector=vector,
            payload={"source": chunk.source, "heading": chunk.heading, "text": chunk.text},
        )
        for chunk, vector in zip(chunks, vectors)
    ]
    c.upsert(collection, points=points, wait=True)


def publish(c: QdrantClient, collection: str) -> list[str]:
    """Point the alias at `collection`, then drop older versions."""
    alias = settings.collection
    actions = [models.CreateAliasOperation(create_alias=models.CreateAlias(collection_name=collection, alias_name=alias))]
    if any(a.alias_name == alias for a in c.get_aliases().aliases):
        actions.insert(0, models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias)))
    c.update_collection_aliases(change_aliases_operations=actions)

    old = [col.name for col in c.get_collections().collections
           if col.name.startswith(f"{alias}-") and col.name != collection]
    for name in old:
        c.delete_collection(name)
    return old


def search(c: QdrantClient, vector: list[float], limit: int) -> list[tuple[Chunk, float]]:
    hits = c.query_points(settings.collection, query=vector, limit=limit, with_payload=True).points
    return [(Chunk(**hit.payload), hit.score) for hit in hits]
