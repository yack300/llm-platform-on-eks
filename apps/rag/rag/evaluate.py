"""Retrieval evaluation: does search find the passage that answers each question?

No model call and no Qdrant server: the corpus is indexed into an in-memory
Qdrant with the same chunking and embeddings as ingestion, every question in
eval/questions.yaml is searched, and a question is a hit when one of its top
k chunks contains its evidence. Deterministic and free, so CI runs it on the
image it's about to publish and fails if the metrics drop below
eval/baseline.json.

    python -m rag.evaluate --questions eval/questions.yaml \\
        [--baseline eval/baseline.json] [--report report.json] [--k 4]

Metrics, over answerable questions (overall, English, Spanish):
  hit@1  the evidence is in the first result
  hit@k  it's somewhere in the top k (what the model gets as context)
  mrr    mean of 1/rank of the first hit (0 if missed): rewards ranking it high
For unanswerable questions it reports top similarity scores, to see whether a
threshold can tell "nothing relevant" apart from a real match.
"""
import argparse
import json
import sys
import time

import yaml
from qdrant_client import QdrantClient, models

from . import embeddings, store
from .config import settings
from .ingest import load_chunks
from .scoring import check_evidence, compare, norm, summarize

COLLECTION = "eval"


def build_index(chunks) -> QdrantClient:
    c = QdrantClient(":memory:")
    c.create_collection(COLLECTION, vectors_config=models.VectorParams(
        size=embeddings.dimension(), distance=models.Distance.COSINE))
    for i in range(0, len(chunks), settings.embed_batch):
        batch = chunks[i:i + settings.embed_batch]
        store.upsert(c, COLLECTION, batch, embeddings.embed([ch.text for ch in batch]))
    return c


def run(questions: list[dict], c: QdrantClient, k: int) -> list[dict]:
    results = []
    for q in questions:
        vector = embeddings.embed_query(q["question"])
        hits = c.query_points(COLLECTION, query=vector, limit=k, with_payload=True).points
        evidence = [norm(e) for e in q.get("evidence", [])]
        rank = next((n for n, h in enumerate(hits, start=1)
                     if any(e in norm(h.payload["text"]) for e in evidence)), None)
        results.append({
            "id": q["id"],
            "lang": q.get("lang", "en"),
            "answerable": q.get("answerable", True),
            "rank": rank,
            "top_score": round(hits[0].score, 3) if hits else 0.0,
            "top_source": f"{hits[0].payload['source']} - {hits[0].payload['heading']}" if hits else "",
        })
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--questions", required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--report")
    parser.add_argument("--k", type=int, default=4)
    args = parser.parse_args()

    questions = yaml.safe_load(open(args.questions))["questions"]
    start = time.monotonic()
    chunks = load_chunks(settings.corpus_dir)
    missing = check_evidence(questions, chunks)
    if missing:
        sys.exit("Evidence not found in the corpus (docs changed? update the set):\n  " + "\n  ".join(missing))

    results = run(questions, build_index(chunks), args.k)
    summary = summarize(results)

    print(f"{'id':30} {'lang':4} {'rank':>4} {'score':>6}  top result")
    for r in results:
        rank = "-" if not r["answerable"] else (r["rank"] or "MISS")
        print(f"{r['id']:30} {r['lang']:4} {rank!s:>4} {r['top_score']:6.3f}  {r['top_source'][:60]}")
    print(f"\nmodel={settings.embedding_model} chunks={len(chunks)} k={args.k} "
          f"({time.monotonic() - start:.0f}s)")
    print(json.dumps(summary, indent=2))

    if args.report:
        with open(args.report, "w") as f:
            json.dump({"model": settings.embedding_model, "k": args.k, "summary": summary,
                       "results": results}, f, indent=2)
    if args.baseline:
        failures = compare(summary, json.load(open(args.baseline)))
        if failures:
            sys.exit("Retrieval regressed below eval/baseline.json:\n  " + "\n  ".join(failures))
        print("Baseline met.")


if __name__ == "__main__":
    main()
