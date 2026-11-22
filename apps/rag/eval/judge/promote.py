#!/usr/bin/env python3
"""Promotion gate: should a candidate model replace the base one?

Compares the judge's verdicts on the same evaluation set answered by the
base model and by a candidate (a LoRA adapter served next to it), and
passes only if the candidate improves the behavior it was trained for and
doesn't get worse at anything else:

  target metric (--target, default language): strictly more checks passed
  every other metric: at most --tolerance fewer checks passed (default 1:
      re-running the same model moves a metric by 2-3 of ~60)
  no ungraded checks in either file (re-run those first, see summary.py)

Exit code 0 to promote, 1 not to; the table goes to stdout as Markdown.
Promoting is then a commit: the RAG API's CHAT_MODEL (k8s/rag/api.yaml)
points at the candidate. Stdlib only.

    python3 apps/rag/eval/judge/promote.py --base base.json [retry.json] \\
        --candidate candidate.json [retry.json]
"""
import argparse
import sys

from summary import METRICS, grade


def totals(paths: list[str]) -> dict:
    _, counts, _, _, grader_errors = grade(paths)
    if grader_errors:
        ids = ",".join(sorted({qid for qid, _, _ in grader_errors}))
        sys.exit(f"{paths[0]}: {len(grader_errors)} ungraded checks ({ids}); re-run them first.")
    return {m: tuple(counts.get(("all", m), (0, 0))) for m in METRICS}


def decide(base: dict, candidate: dict, target: str, tolerance: int) -> list[tuple]:
    """(metric, base, candidate, rule, ok) per metric."""
    rows = []
    for m in METRICS:
        (b, bn), (c, cn) = base[m], candidate[m]
        if bn != cn:
            rows.append((m, f"{b}/{bn}", f"{c}/{cn}", "same number of checks", False))
        elif m == target:
            rows.append((m, f"{b}/{bn}", f"{c}/{cn}", "must improve", c > b))
        else:
            rows.append((m, f"{b}/{bn}", f"{c}/{cn}", f"at most {tolerance} fewer", c >= b - tolerance))
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", nargs="+", required=True)
    p.add_argument("--candidate", nargs="+", required=True)
    p.add_argument("--target", default="language", choices=METRICS)
    p.add_argument("--tolerance", type=int, default=1)
    args = p.parse_args()

    rows = decide(totals(args.base), totals(args.candidate), args.target, args.tolerance)
    print("| check | base | candidate | rule | |")
    print("|---|---|---|---|---|")
    for m, b, c, rule, ok in rows:
        print(f"| {m} | {b} | {c} | {rule} | {'pass' if ok else '**fail**'} |")
    promote = all(ok for *_, ok in rows)
    print(f"\n**{'Promote' if promote else 'Do not promote'}** the candidate.")
    sys.exit(0 if promote else 1)


if __name__ == "__main__":
    main()
