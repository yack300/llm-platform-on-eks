#!/usr/bin/env python3
"""Measures a full cold start: from a request with vLLM and the GPU node
group at zero to the first answer served by vLLM.

It sends one request through LiteLLM (which KEDA sees and scales vLLM 0->1;
the client itself gets claude-fallback's answer), waits for the vLLM pod to
be Ready, retries with fallbacks disabled until vLLM answers, and then
rebuilds the timeline from the Kubernetes objects: pod created (KEDA),
NodeClaim created (Karpenter's launch decision), node created (EC2 boot),
node Ready, image pull, container start, pod Ready. It also prints vLLM's own startup log lines (weights download and
load, CUDA graph capture) to break the last stretch down further.

Standard library only; uses kubectl's current context. Needs the gateway
reachable, e.g.:
  kubectl -n llm-platform port-forward svc/litellm-gateway 4000:4000

Usage:
  ./scripts/measure-cold-start.py
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from benchmark import master_key  # noqa: E402

NAMESPACE = "llm-platform"
POD_SELECTOR = "app=vllm-inference"
GPU_NODE_SELECTOR = "karpenter.sh/nodepool=gpu"
VLLM_LOG_PATTERNS = [
    r"Loading weights took", r"Model loading took", r"[Gg]raph capturing finished",
    r"init engine .* took", r"Starting vLLM API server", r"Application startup complete",
]


def kubectl_json(*args) -> dict:
    out = subprocess.run(["kubectl", *args, "-o", "json"], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def condition(obj: dict, kind: str):
    for c in obj.get("status", {}).get("conditions", []):
        if c["type"] == kind and c["status"] == "True":
            return ts(c["lastTransitionTime"])
    return None


def chat(url: str, key: str, model: str, disable_fallbacks: bool):
    body = {"model": model, "messages": [{"role": "user", "content": "Say hi."}], "max_tokens": 8}
    if disable_fallbacks:
        body["disable_fallbacks"] = True
    req = urllib.request.Request(
        f"{url}/chat/completions", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    # Timestamps come from the gateway's Date header, not this machine's
    # clock, so they line up with the cluster's own timestamps (a laptop
    # clock a few seconds off put the first answer before pod Ready).
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read()).get("model"), server_time(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, None, server_time(e.headers)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return None, str(e), datetime.now(timezone.utc)


def server_time(headers) -> datetime:
    date = headers.get("Date") if headers else None
    return parsedate_to_datetime(date) if date else datetime.now(timezone.utc)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("LITELLM_URL", "http://localhost:4000/v1"))
    parser.add_argument("--model", default="local-mistral")
    parser.add_argument("--timeout-min", type=int, default=30)
    parser.add_argument("--force", action="store_true", help="run even if vLLM or a GPU node is already up")
    args = parser.parse_args()

    pods = kubectl_json("-n", NAMESPACE, "get", "pods", "-l", POD_SELECTOR)["items"]
    nodes = kubectl_json("get", "nodes", "-l", GPU_NODE_SELECTOR)["items"]
    if (pods or nodes) and not args.force:
        sys.exit(f"Not a cold start: {len(pods)} vLLM pod(s), {len(nodes)} GPU node(s). "
                 "Wait for scale-to-zero (vLLM ~5 min, GPU node ~18 min idle) or pass --force.")

    key = master_key()
    status, served_by, t0 = chat(args.url, key, args.model, disable_fallbacks=False)
    print(f"{t0:%H:%M:%S} wake-up request sent: HTTP {status}, answered by {served_by}")

    deadline = time.monotonic() + args.timeout_min * 60
    pod = None
    last_report = 0.0
    while time.monotonic() < deadline:
        pods = kubectl_json("-n", NAMESPACE, "get", "pods", "-l", POD_SELECTOR)["items"]
        pod = pods[0] if pods else None
        if pod and condition(pod, "Ready"):
            break
        if time.monotonic() - last_report > 30:
            elapsed = (datetime.now(timezone.utc) - t0).total_seconds()  # progress only
            state = pod["status"].get("phase") if pod else "no pod yet"
            print(f"  +{elapsed:5.0f}s  vLLM pod: {state}", flush=True)
            last_report = time.monotonic()
        time.sleep(5)
    else:
        sys.exit("Timed out waiting for the vLLM pod to be Ready")

    # LiteLLM may still have the deployment in cooldown after the failed
    # attempts; retry until vLLM itself answers.
    while time.monotonic() < deadline:
        status, served_by, t_answer = chat(args.url, key, args.model, disable_fallbacks=True)
        if status == 200:
            break
        time.sleep(2)

    name = pod["metadata"]["name"]
    node = kubectl_json("get", "node", pod["spec"]["nodeName"])
    events = kubectl_json("-n", NAMESPACE, "get", "events",
                          "--field-selector", f"involvedObject.name={name}")["items"]

    def event_time(reason: str):
        for e in events:
            if e.get("reason") == reason:
                return ts(e.get("eventTime") or e.get("firstTimestamp") or e["lastTimestamp"]), e.get("message", "")
        return None, ""

    # The NodeClaim Karpenter created for this node: when it decided to launch.
    claims = kubectl_json("get", "nodeclaims", "-l", GPU_NODE_SELECTOR)["items"]
    claim = next((c for c in claims if c.get("status", {}).get("nodeName") == pod["spec"]["nodeName"]), None)
    capacity = node["metadata"]["labels"].get("karpenter.sh/capacity-type", "?")

    pulling, _ = event_time("Pulling")
    pulled, pulled_msg = event_time("Pulled")
    started = pod["status"]["containerStatuses"][0]["state"].get("running", {}).get("startedAt")

    steps = [
        ("vLLM pod created (KEDA 0->1)", ts(pod["metadata"]["creationTimestamp"])),
        ("Karpenter NodeClaim created", ts(claim["metadata"]["creationTimestamp"]) if claim else None),
        (f"GPU node object created ({capacity})", ts(node["metadata"]["creationTimestamp"])),
        ("GPU node Ready", condition(node, "Ready")),
        ("pod scheduled on the node", condition(pod, "PodScheduled")),
        ("image pull started", pulling),
        ("image pull finished", pulled),
        ("container started", ts(started) if started else None),
        ("pod Ready (/health)", condition(pod, "Ready")),
        ("first answer from vLLM", t_answer),
    ]
    print(f"\nCold start timeline (pod {name}, node {node['metadata']['name']}):")
    prev = t0
    for label, when in steps:
        if when is None:
            print(f"  {'':>8}  {'':>8}  {label}: not found")
            continue
        print(f"  +{(when - t0).total_seconds():6.0f}s  (+{(when - prev).total_seconds():4.0f}s)  {label}")
        prev = when
    if pulled_msg:
        print(f"\n  kubelet: {pulled_msg}")

    logs = subprocess.run(["kubectl", "-n", NAMESPACE, "logs", name], capture_output=True, text=True).stdout
    lines = [ln for ln in logs.splitlines() if any(re.search(p, ln) for p in VLLM_LOG_PATTERNS)]
    if lines:
        print("\nvLLM startup log:")
        for ln in lines:
            print(f"  {ln.strip()[:200]}")


if __name__ == "__main__":
    main()
