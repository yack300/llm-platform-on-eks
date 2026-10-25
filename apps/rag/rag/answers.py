"""Answers every question in the evaluation set through the running API, for
the answer-quality judge (eval/judge/, docs/retrieval-eval.md).

Runs inside the rag-api pod, so it needs no port-forward and nothing
installed on the laptop; the question set comes in on stdin and the answers
go out on stdout:

    kubectl -n llm-platform exec -i deploy/rag-api -- python -m rag.answers \\
        < apps/rag/eval/questions.yaml > apps/rag/eval/answers/<date>.json

Questions go one at a time, like a user would ask them. Each answer records
the model that produced it: while vLLM cold-starts LiteLLM falls back to
claude-fallback, and those answers say nothing about the local model.
"""
import argparse
import collections
import datetime
import json
import sys
import time

import httpx
import yaml


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8080/ask")
    parser.add_argument("--top-k", type=int, default=4)
    args = parser.parse_args()

    questions = yaml.safe_load(sys.stdin)["questions"]
    answers = []
    with httpx.Client(timeout=180) as http:
        for q in questions:
            start = time.perf_counter()
            resp = http.post(args.url, json={"question": q["question"], "top_k": args.top_k,
                                             "include_context": True})
            seconds = round(time.perf_counter() - start, 2)
            if resp.status_code != 200:
                print(f"{q['id']}: HTTP {resp.status_code} {resp.text[:200]}", file=sys.stderr)
                answers.append({"id": q["id"], "error": f"HTTP {resp.status_code}"})
                continue
            body = resp.json()
            answers.append({
                "id": q["id"],
                "lang": q.get("lang", "en"),
                "answerable": q.get("answerable", True),
                "question": q["question"],
                "reference": q.get("reference", ""),
                "answer": body["answer"],
                "model": body["model"],
                "seconds": seconds,
                "contexts": body["sources"],
            })
            print(f"{q['id']}: {body['model']} {seconds}s", file=sys.stderr)

    models = collections.Counter(a.get("model", "error") for a in answers)
    print(f"answers by model: {dict(models)}", file=sys.stderr)
    json.dump({"date": datetime.date.today().isoformat(), "top_k": args.top_k, "answers": answers},
              sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
