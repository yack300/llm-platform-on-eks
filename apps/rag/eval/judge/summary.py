#!/usr/bin/env python3
"""Pass rate per judge metric, by language and by the model that answered,
from promptfoo's results file, plus every failed check with the judge's
reason. For calibration.json, whose answers carry the expected verdicts, it
also reports where the judge disagreed. Markdown, for the terminal or a CI
job summary. Stdlib only.

    python3 apps/rag/eval/judge/summary.py apps/rag/eval/judge/results.json
"""
import collections
import json
import sys

METRICS = ["correct", "faithful", "declines", "language"]


def main(path: str) -> None:
    results = json.load(open(path))["results"]["results"]
    # (group, metric) -> [passed, total]
    counts = collections.defaultdict(lambda: [0, 0])
    failures = []
    disagreements = []
    grader_errors = []
    for r in results:
        meta = r["testCase"].get("metadata", {})
        groups = ["all", f"lang={meta.get('lang')}", f"model={meta.get('model')}"]
        if not r.get("gradingResult"):  # failed before grading (e.g. template error)
            grader_errors.append((meta.get("id"), "all", (r.get("error") or "").split("\n")[0]))
            continue
        for c in r["gradingResult"]["componentResults"]:
            metric = c["assertion"].get("metric")
            reason = c.get("reason", "").replace("\n", " ")
            # The judge itself failed (API error, no credit...): not a verdict.
            if c.get("metadata", {}).get("graderError"):
                grader_errors.append((meta.get("id"), metric, reason))
                continue
            for g in groups:
                counts[g, metric][0] += c["pass"]
                counts[g, metric][1] += 1
            if not c["pass"]:
                failures.append((meta.get("id"), metric, reason))
            expected = json.loads(meta["expect"]).get(metric) if "expect" in meta else None
            if expected is not None and expected != c["pass"]:
                disagreements.append((meta.get("id"), metric, reason))

    if grader_errors:
        sys.exit(f"The judge couldn't grade {len(grader_errors)} checks, e.g. "
                 f"{grader_errors[0][0]} {grader_errors[0][1]}: {grader_errors[0][2][:300]}")

    groups = sorted({g for g, _ in counts}, key=lambda g: (g != "all", g))
    print("| | " + " | ".join(METRICS) + " |")
    print("|---" * (len(METRICS) + 1) + "|")
    for g in groups:
        cells = []
        for m in METRICS:
            passed, total = counts.get((g, m), (0, 0))
            cells.append(f"{passed}/{total}" if total else "-")
        print(f"| {g} | " + " | ".join(cells) + " |")
    if failures:
        print(f"\n{len(failures)} failed checks:\n")
        for qid, metric, reason in failures:
            print(f"- **{qid}** {metric}: {reason[:300]}")
    if any("expect" in r["testCase"].get("metadata", {}) for r in results):
        print(f"\nCalibration: the judge disagreed with the expected verdict on {len(disagreements)} checks.")
        for qid, metric, reason in disagreements:
            print(f"- **{qid}** {metric}: {reason[:300]}")
        if disagreements:
            sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1])
