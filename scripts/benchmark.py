#!/usr/bin/env python3
"""Throughput and latency benchmark through the LiteLLM gateway.

For each concurrency level, N workers send streaming chat requests in
parallel and the script reports time to first token (TTFT), end-to-end
latency and aggregate output tokens/s. Requests go through LiteLLM, like
real clients, with fallbacks disabled so a cold or failing vLLM shows up as
errors instead of silently being answered by claude-fallback.

Standard library only. Needs the gateway reachable, e.g.:
  kubectl -n llm-platform port-forward svc/litellm-gateway 4000:4000

Usage:
  ./scripts/benchmark.py                      # concurrency 1, 4, 8
  ./scripts/benchmark.py --concurrency 1,8 --requests-per-worker 8 --json out.json
"""
import argparse
import base64
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PROMPT = "Explain in a few paragraphs how Kubernetes schedules a pod onto a node."


def master_key() -> str:
    """LITELLM_API_KEY, or the master key from the cluster's litellm-secrets."""
    key = os.environ.get("LITELLM_API_KEY")
    if key:
        return key
    out = subprocess.run(
        ["kubectl", "-n", "llm-platform", "get", "secret", "litellm-secrets",
         "-o", "jsonpath={.data.LITELLM_MASTER_KEY}"],
        capture_output=True, text=True, check=True,
    ).stdout
    return base64.b64decode(out).decode()


def one_request(url: str, key: str, model: str, max_tokens: int) -> dict:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
        # LiteLLM: fail instead of routing to claude-fallback.
        "disable_fallbacks": True,
    }).encode()
    req = urllib.request.Request(
        f"{url}/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    start = time.perf_counter()
    ttft = None
    tokens = 0
    chunks = 0
    served_by = None
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            for raw in resp:
                line = raw.decode().strip()
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                event = json.loads(line[len("data: "):])
                served_by = event.get("model", served_by)
                if event.get("usage"):
                    tokens = event["usage"].get("completion_tokens", tokens)
                for choice in event.get("choices", []):
                    if choice.get("delta", {}).get("content"):
                        chunks += 1
                        if ttft is None:
                            ttft = time.perf_counter() - start
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": ttft is not None,
        "ttft": ttft,
        "latency": time.perf_counter() - start,
        # Fall back to counting chunks if the usage chunk didn't arrive.
        "tokens": tokens or chunks,
        "model": served_by,
    }


def pct(values: list, p: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, round(p / 100 * (len(values) - 1)))]


def run_level(args, key: str, concurrency: int) -> dict:
    results = []
    lock = threading.Lock()

    def worker():
        for _ in range(args.requests_per_worker):
            r = one_request(args.url, key, args.model, args.max_tokens)
            with lock:
                results.append(r)

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for _ in range(concurrency):
            pool.submit(worker)
    wall = time.perf_counter() - start

    ok = [r for r in results if r["ok"]]
    if not ok:
        return {"concurrency": concurrency, "requests": len(results), "errors": len(results)}
    ttfts = [r["ttft"] for r in ok]
    lats = [r["latency"] for r in ok]
    tokens = sum(r["tokens"] for r in ok)
    return {
        "concurrency": concurrency,
        "requests": len(results),
        "errors": len(results) - len(ok),
        "served_by": sorted({str(r["model"]) for r in ok}),
        "output_tokens_per_s": tokens / wall,
        "requests_per_s": len(ok) / wall,
        "avg_output_tokens": tokens / len(ok),
        "ttft_p50_s": statistics.median(ttfts),
        "ttft_p95_s": pct(ttfts, 95),
        "latency_p50_s": statistics.median(lats),
        "latency_p95_s": pct(lats, 95),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("LITELLM_URL", "http://localhost:4000/v1"))
    parser.add_argument("--model", default="local-mistral")
    parser.add_argument("--concurrency", default="1,4,8", help="comma-separated levels")
    parser.add_argument("--requests-per-worker", type=int, default=4)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--json", help="also write the results to this file")
    args = parser.parse_args()

    key = master_key()
    print("Warm-up request...", flush=True)
    warm = one_request(args.url, key, args.model, args.max_tokens)
    if not warm["ok"]:
        sys.exit(f"Warm-up failed, is vLLM up and the gateway port-forwarded? {warm.get('error', '')}")
    print(f"Served by: {warm['model']}\n")

    levels = []
    header = f"{'conc':>4} {'reqs':>5} {'err':>4} {'tok/s':>8} {'req/s':>6} {'TTFT p50':>9} {'TTFT p95':>9} {'lat p50':>8} {'lat p95':>8}"
    print(header)
    for c in (int(x) for x in args.concurrency.split(",")):
        r = run_level(args, key, c)
        levels.append(r)
        if "output_tokens_per_s" not in r:
            print(f"{c:>4} {r['requests']:>5} {r['errors']:>4}  all requests failed")
            continue
        print(f"{c:>4} {r['requests']:>5} {r['errors']:>4} {r['output_tokens_per_s']:>8.1f} "
              f"{r['requests_per_s']:>6.2f} {r['ttft_p50_s']:>8.2f}s {r['ttft_p95_s']:>8.2f}s "
              f"{r['latency_p50_s']:>7.2f}s {r['latency_p95_s']:>7.2f}s", flush=True)

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"model": args.model, "max_tokens": args.max_tokens,
                       "prompt": PROMPT, "levels": levels}, f, indent=2)
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    main()
