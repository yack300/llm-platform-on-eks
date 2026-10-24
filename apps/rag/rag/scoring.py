"""Pure scoring for rag.evaluate: no model, no Qdrant, so it's unit tested."""
import statistics


def norm(text: str) -> str:
    """Case- and whitespace-insensitive, so evidence still matches when the
    docs wrap a phrase across lines."""
    return " ".join(text.split()).lower()


def check_evidence(questions: list[dict], chunks) -> list[str]:
    corpus = norm(" ".join(c.text for c in chunks))
    return [f"{q['id']}: {e!r}" for q in questions for e in q.get("evidence", []) if norm(e) not in corpus]


def metrics(rows: list[dict]) -> dict:
    if not rows:
        return {}
    return {
        "n": len(rows),
        "hit@1": round(statistics.mean(r["rank"] == 1 for r in rows), 3),
        "hit@k": round(statistics.mean(r["rank"] is not None for r in rows), 3),
        "mrr": round(statistics.mean(1 / r["rank"] if r["rank"] else 0 for r in rows), 3),
    }


def summarize(results: list[dict]) -> dict:
    answerable = [r for r in results if r["answerable"]]
    unanswerable = [r for r in results if not r["answerable"]]
    hit_scores = [r["top_score"] for r in answerable if r["rank"]]
    summary = {
        "all": metrics(answerable),
        "en": metrics([r for r in answerable if r["lang"] == "en"]),
        "es": metrics([r for r in answerable if r["lang"] == "es"]),
    }
    if unanswerable and hit_scores:
        # A threshold between these two would reject every unanswerable
        # question and keep every answerable hit; negative means overlap.
        summary["threshold"] = {
            "max_unanswerable_score": max(r["top_score"] for r in unanswerable),
            "min_hit_score": min(hit_scores),
            "separation": round(min(hit_scores) - max(r["top_score"] for r in unanswerable), 3),
        }
    return summary


def compare(summary: dict, baseline: dict) -> list[str]:
    """Baseline minimums are 'group.metric' keys, e.g. {"all.hit@k": 0.9}."""
    failures = []
    for key, minimum in baseline.get("min", {}).items():
        group, metric = key.split(".")
        value = summary.get(group, {}).get(metric)
        if value is None or value < minimum:
            failures.append(f"{key} = {value} < {minimum}")
    return failures
