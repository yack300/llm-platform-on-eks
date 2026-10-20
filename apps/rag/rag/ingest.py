"""Ingestion: corpus Markdown -> chunks -> embeddings -> new Qdrant version.

Runs as a Kubernetes Job (k8s/rag/ingest-job.yaml). The corpus is the
project's own documentation, copied into the image at build time, so the
index always matches the image version that serves it.

    python -m rag.ingest
"""
import logging
import time
from pathlib import Path

from . import embeddings, store
from .chunking import chunk_document
from .config import settings

log = logging.getLogger("rag.ingest")
BATCH = 32


def load_chunks(corpus_dir: str):
    root = Path(corpus_dir)
    chunks = []
    for path in sorted(root.rglob("*.md")):
        source = str(path.relative_to(root))
        chunks.extend(chunk_document(source, path.read_text(encoding="utf-8"),
                                     settings.chunk_size, settings.chunk_overlap))
    return chunks


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per Qdrant call
    start = time.monotonic()

    chunks = load_chunks(settings.corpus_dir)
    if not chunks:
        raise SystemExit(f"No Markdown found under {settings.corpus_dir}")
    log.info("%d chunks from %d files", len(chunks), len({c.source for c in chunks}))

    c = store.client()
    store.wait_until_ready(c)
    collection = store.create_version(c, embeddings.dimension())

    for i in range(0, len(chunks), BATCH):
        batch = chunks[i:i + BATCH]
        store.upsert(c, collection, batch, embeddings.embed([ch.text for ch in batch]))

    removed = store.publish(c, collection)
    log.info("alias %s -> %s (removed %s) in %.1fs", settings.collection, collection,
             removed or "nothing", time.monotonic() - start)


if __name__ == "__main__":
    main()
