# Cost and performance — results from a real EKS run

Measured on 2026-10-06 on a real EKS 1.36 cluster built from this repo, then torn
down the same day. Everything below comes from that single session; rerun it
before relying on the numbers for anything else.

## Setup

| | |
|---|---|
| GPU node | `g4dn.xlarge` (1x NVIDIA T4, 16 GB), **on-demand**: spot had no capacity that day (placement score 1 in every AZ) |
| Model | `TheBloke/Mistral-7B-Instruct-v0.2-AWQ` (4-bit AWQ), served by vLLM as `local-mistral` |
| Path | client → LiteLLM gateway → vLLM (every request went through the gateway) |
| Workload | short prompts (~17 input tokens), `max_tokens: 128` (~128 output tokens per request) |
| Prices used | on-demand $0.526/h; spot ~$0.27/h (us-east-1 price at the time, for comparison) |

## Throughput and latency

| Concurrent requests | Output tokens/s | Requests/s | p50 latency |
|---|---|---|---|
| 1 | 50 | 0.39 | 2.54 s |
| 4 | 186 | 1.45 | 2.75 s |
| 8 | 342 | 2.67 | 2.98 s |

Going from 1 to 8 concurrent requests multiplies throughput by ~6.8x while p50
latency only grows from 2.5 s to 3 s: vLLM's continuous batching keeps the same
GPU busy with several requests at once.

## Cost (GPU node only)

| Concurrent requests | Per 1M output tokens (on-demand) | Per 1M output tokens (spot) | Per 1,000 requests (on-demand) | Per 1,000 requests (spot) |
|---|---|---|---|---|
| 1 | $2.90 | $1.49 | $0.37 | $0.19 |
| 4 | $0.79 | $0.40 | $0.10 | $0.05 |
| 8 | $0.43 | $0.22 | $0.055 | $0.028 |

Computed as `hourly price ÷ (tokens or requests per hour)` at sustained load.
These figures cover only the GPU node. They leave out the fixed cost of
running the platform: the EKS control plane ($0.10/h) and the spot platform
nodes that run LiteLLM, Prometheus, Argo CD and the rest, which bill even
when vLLM is scaled to zero.

## Scale-to-zero, measured

Waking from zero, with no GPU node running:

| Step | Time |
|---|---|
| KEDA sees the LiteLLM request and scales vLLM 0 → 1 | ~18 s |
| Cluster Autoscaler triggers a GPU node scale-up from 0 | ~15 s |
| GPU node boots and joins the cluster | ~1.5 min |
| vLLM image pull (8.7 GB) | ~4.5 min |
| Model load + CUDA graph capture | ~2 min |
| **First local response** | **~8–9 min** |

During that window LiteLLM's `fallbacks` send `local-mistral` requests to
`claude-fallback`, so clients still get an answer.

Going back to zero after the last request:

- vLLM at 0 replicas: ~5 min.
- GPU node removed by the Cluster Autoscaler: ~18 min.

The cold start is dominated by the image pull. The obvious next improvements
are caching the vLLM image on the node (a custom AMI or a pre-pulled image)
and keeping the model weights on a volume instead of downloading them on
every start.

## What the whole session cost

The full AWS bill for the day (Cost Explorer), from `terraform apply` through
the benchmark, the scale-to-zero test and the teardown, ~2 hours of cluster time:

| Service | Cost |
|---|---|
| EC2 instances (platform spot nodes + GPU node) | $0.61 |
| EC2 other (EBS volumes) | $0.33 |
| KMS (includes a key not created by this project) | $0.30 |
| EKS control plane | $0.22 |
| VPC (public IPv4 addresses) | $0.03 |
| S3 (Terraform state) | < $0.01 |
| **Total before tax** | **$1.48** |

## Conclusion

The cost per token depends almost entirely on utilization. The same GPU costs
$2.90 per million output tokens with one request at a time and $0.43 with
eight. And while it sits idle, the per-token cost is effectively unbounded:
a `g4dn.xlarge` on-demand kept up 24/7 costs ~$384/month whether it serves
anything or not.

That shapes the design:

- **Low or bursty traffic:** scale vLLM and its GPU node to zero, accept an
  ~8-minute cold start, and let the gateway route to an external provider
  meanwhile. You pay for the GPU only while there's demand.
- **Sustained traffic with enough concurrency to batch:** keeping the GPU
  warm pays off, and spot capacity roughly halves the cost when it's available.

Where the line falls between the two depends on the external provider's
current per-token pricing and on your own traffic. The Grafana dashboard
`k8s/observability/grafana-dashboard-llm-cost.yaml` computes both sides live:
set the provider's API prices and the GPU node's hourly cost in its variables.
