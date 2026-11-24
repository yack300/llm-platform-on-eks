#!/usr/bin/env python3
"""GPU cost per team, from OpenCost (docs/gpu-teams.md).

Reads OpenCost's allocations, and the nodes' uptime and price, over a window and prints, per GPU
node, who used it and what they owe. OpenCost bills every time-sliced share
as a whole GPU, so this doesn't use its GPU cost: a team owes the node's
price for the fraction of its shares the team held. Shares nobody held are
owed by the team that owns that capacity type. Both totals are printed, with
the nodes' real cost next to them.

  kubectl -n opencost port-forward svc/opencost 9003:9003
  kubectl -n monitoring port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090
  ./scripts/gpu-chargeback.py --window 1h
"""
import argparse
import datetime as dt
import json
import urllib.parse
import urllib.request

TEAMS = {"llm-platform": "product", "training": "research"}
# Who pays for a GPU while it sits idle: the team whose quota it is
# (k8s/teams/queues.yaml).
OWNERS = {"on-demand": "product", "spot": "research"}
CAPACITY_LABEL = "karpenter_sh_capacity_type"
REPLICAS_LABEL = "nvidia_com_gpu_replicas"  # set by the gpu-shared NodePool
STEP = 15  # seconds between samples when measuring how long a node lived


def chargeback(allocations, nodes):
    """What each team owes per GPU node.

    allocations: [{namespace, node, gpu_hours, gpu_cost}], gpu_hours counted
    in shares, as requested. nodes: {name: {hours, price, shares, capacity}},
    price being hourly and shares how many the node's GPU is sliced in (1 if
    it isn't). Returns one row per (node, team) plus the
    idle remainder, and the totals.
    """
    rows = []
    for name, node in sorted(nodes.items()):
        shares = node["shares"]
        share_hours = node["hours"] * shares
        used = {}
        for a in allocations:
            if a["node"] != name or not a["gpu_hours"]:
                continue
            team = TEAMS.get(a["namespace"], a["namespace"])
            u = used.setdefault(team, {"share_hours": 0.0, "opencost": 0.0})
            u["share_hours"] += a["gpu_hours"]
            u["opencost"] += a["gpu_cost"]
        for team, u in sorted(used.items()):
            rows.append({
                "node": name, "capacity": node["capacity"], "team": team, "kind": "used",
                "share_hours": u["share_hours"], "opencost": u["opencost"],
                "owed": u["share_hours"] / shares * node["price"],
            })
        # Scrape timing can put the used hours slightly over the node's own
        idle = max(0.0, share_hours - sum(u["share_hours"] for u in used.values()))
        rows.append({
            "node": name, "capacity": node["capacity"],
            "team": OWNERS.get(node["capacity"], "unassigned"), "kind": "idle",
            "share_hours": idle, "opencost": 0.0, "owed": idle / shares * node["price"],
        })
    totals = {}
    for r in rows:
        t = totals.setdefault(r["team"], {"used": 0.0, "idle": 0.0, "opencost": 0.0})
        t[r["kind"]] += r["owed"]
        t["opencost"] += r["opencost"]
    real = sum(n["hours"] * n["price"] for n in nodes.values())
    return rows, totals, real


def get(url, **params):
    with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}", timeout=60) as r:
        return json.load(r)["data"]


def parse_window(text):
    """(start, end) from "90m", "6h", "2d" or "<start>,<end>" in RFC3339."""
    if "," in text:
        return tuple(dt.datetime.fromisoformat(t.replace("Z", "+00:00")) for t in text.split(","))
    end = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    unit = {"m": "minutes", "h": "hours", "d": "days"}[text[-1]]
    return end - dt.timedelta(**{unit: int(text[:-1])}), end


def fetch_allocations(opencost, start, end):
    window = ",".join(t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in (start, end))
    data = get(f"{opencost}/allocation", window=window, aggregate="namespace,node", accumulate="true")
    return [
        {"namespace": a["properties"].get("namespace", ""), "node": a["properties"].get("node", ""),
         "gpu_hours": a["gpuHours"], "gpu_cost": a["gpuCost"]}
        for a in (data[0] or {}).values()
    ]


def fetch_nodes(prometheus, start, end, prices_by_capacity):
    """GPU nodes' uptime, price, shares and capacity type. From OpenCost's metrics
    in Prometheus: its /assets API gave nodes 0 minutes in short windows."""
    span = f"[{int((end - start).total_seconds())}s:{STEP}s]"

    def query(q):
        result = get(f"{prometheus}/api/v1/query", query=q, time=end.timestamp())["result"]
        return {r["metric"]["node"]: r for r in result}

    samples = query(f"count_over_time((max by (node) (node_gpu_count > 0)){span})")
    prices = query(f"avg_over_time((max by (node) (node_gpu_hourly_cost)){span})")
    counts = query(f"avg_over_time((max by (node) (node_gpu_count > 0)){span})")
    labels = query(f"max_over_time((max by (node, label_{CAPACITY_LABEL}, label_{REPLICAS_LABEL}) (kube_node_labels)){span})")
    nodes = {}
    for name, r in samples.items():
        meta = labels[name]["metric"]
        capacity = meta.get(f"label_{CAPACITY_LABEL}", "unknown")
        nodes[name] = {
            "hours": int(r["value"][1]) * STEP / 3600,
            "price": prices_by_capacity.get(capacity, float(prices[name]["value"][1])),
            "shares": int(meta.get(f"label_{REPLICAS_LABEL}", 1)),
            "capacity": capacity,
            "counted_gpus": float(counts[name]["value"][1]),
        }
    return nodes


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--opencost", default="http://localhost:9003")
    p.add_argument("--prometheus", default="http://localhost:9090")
    p.add_argument("--window", default="1h", help="e.g. 90m, 6h, 2d, or <start>,<end> in RFC3339")
    p.add_argument("--price", action="append", default=[], metavar="CAPACITY=USD",
                   help="whole-node hourly price, e.g. on-demand=0.526 (default: OpenCost's GPU price only)")
    p.add_argument("--json", help="also write the raw inputs and results here")
    args = p.parse_args()

    start, end = parse_window(args.window)
    allocations = fetch_allocations(args.opencost, start, end)
    nodes = fetch_nodes(args.prometheus, start, end,
                        {k: float(v) for k, v in (x.split("=") for x in args.price)})
    if not nodes:
        raise SystemExit("No GPU nodes in that window.")
    rows, totals, real = chargeback(allocations, nodes)

    print("| node | capacity | team | | share-hours | OpenCost | owed |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['node']} | {r['capacity']} | {r['team']} | {r['kind']} "
              f"| {r['share_hours']:.3f} | ${r['opencost']:.4f} | ${r['owed']:.4f} |")
    print("\n| team | used | idle | owed | OpenCost says |\n|---|---|---|---|---|")
    for team, t in sorted(totals.items()):
        print(f"| {team} | ${t['used']:.4f} | ${t['idle']:.4f} "
              f"| **${t['used'] + t['idle']:.4f}** | ${t['opencost']:.4f} |")
    owed = sum(t["used"] + t["idle"] for t in totals.values())
    billed = sum(t["opencost"] for t in totals.values())
    print(f"\nGPU nodes' real cost ${real:.4f}; owed ${owed:.4f}; OpenCost's allocations ${billed:.4f}")
    print("GPUs OpenCost counts per node: "
          + ", ".join(f"{n} {v['counted_gpus']:g}" for n, v in sorted(nodes.items())))
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"window": args.window, "allocations": allocations,
                       "nodes": nodes, "rows": rows, "totals": totals, "real": real}, f, indent=1)


if __name__ == "__main__":
    main()
