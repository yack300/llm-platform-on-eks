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

## Scale-to-zero, measured (first run)

Waking from zero, with no GPU node running (AL2023 GPU nodes):

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

## Second run (2026-10-13): trying to cut the cold start

Goal: remove the image pull by creating the GPU nodes' data volume from an
EBS snapshot with the vLLM image already pulled (Bottlerocket GPU nodes,
`scripts/build-gpu-image-cache.sh`). Every number below comes from
`scripts/measure-cold-start.py` with a single request, from zero vLLM pods
and zero GPU nodes. Same cluster, same day, on-demand `g4dn.xlarge`.

| | No cache (baseline) | Snapshot | Snapshot + 300 MiB/s init rate |
|---|---|---|---|
| Request → GPU node Ready | 1:31 | 1:42 | 1:48 |
| Image pull | 4:24 | 0 (already on disk) | 0 (already on disk) |
| Container start → vLLM Ready | 4:12 | 11:58 | 13:20 |
| **Request → first vLLM answer** | **~10 min** | **~14.5 min** | **~15.8 min** |

The snapshot made the cold start slower. A volume restored from a snapshot
fetches each block from S3 the first time it's read, and vLLM reads
thousands of PyTorch/CUDA files at startup: during the slow runs the
container used under 0.6 CPU cores with no throttling, and the volume served
only ~40-80 reads/s, waiting on S3 rather than hitting gp3 limits. After 15
minutes the volume was 21% initialized.

The fix should have been a provisioned volume initialization rate (the whole
~30 GiB snapshot in ~100 s), but EKS managed node groups copy the launch
template into their own and drop `VolumeInitializationRate`: ours had 300,
the copy EKS launches from doesn't, so the third run behaved like the
second. Fast snapshot restore would work but bills per hour per AZ even when
nothing is restored (~$540/month per AZ), which defeats scale-to-zero.

What would actually help, for a next run: nodes that honor the launch
template as written (Karpenter or a self-managed node group) so the
initialization rate applies, or pulling the image from ECR in the same region
(pull-through cache) instead of Docker Hub. The repo went with the first:
GPU nodes now come from Karpenter (`k8s/karpenter/`), which also falls back
from spot to on-demand and launches nodes in the AZ of a pod's volume.

Other findings from this run:

- **Bottlerocket GPU nodes join faster:** Ready ~15 s after the EC2 node
  object appears, ~50 s after the scale-up, vs ~1.5 min for AL2023.
- **vLLM's own startup is now ~4 min, not ~2:** vLLM v0.30 adds a
  `torch.compile` step (~27 s warm, ~44 s on the cold disk) and captures
  CUDA graphs twice. That's the next target after the image pull.
- **KEDA was scaling vLLM to zero mid-startup** (fixed): with a single
  request, the demand trigger expires after ~7 minutes, before a 10-minute
  cold start finishes. A `startup-in-progress` trigger now holds the replica
  while its pod isn't Ready, for up to 20 minutes.
- **Spot interruption vs. persistent volumes:** a platform spot node was
  replaced in a different AZ and Qdrant/Grafana stayed Pending, since their
  EBS volumes can't follow them across AZs. Not fixed: stateful workloads
  need a node group per AZ, or volumes that can be recreated.

Warm performance (`scripts/benchmark.py`, streaming through LiteLLM,
128 output tokens per request):

| Concurrent requests | Output tokens/s | TTFT p50 | TTFT p95 | Latency p50 | Latency p95 |
|---|---|---|---|---|---|
| 1 | 47 | 0.31 s | 0.31 s | 2.74 s | 2.74 s |
| 4 | 172 | 0.36 s | 0.42 s | 2.97 s | 3.02 s |
| 8 | 310 | 0.41 s | 0.45 s | 3.26 s | 3.36 s |

Throughput is ~9% below the first run's (342 tok/s at 8); the prompt,
streaming and node differ between runs, so treat it as run-to-run noise
rather than a regression.

What this run cost (Cost Explorer, ~2.5 hours of cluster time, three cold
starts, the snapshot builds and the teardown):

| Service | Cost |
|---|---|
| EC2 instances (platform spot nodes, GPU node, snapshot builder) | $0.91 |
| EKS control plane | $0.24 |
| EC2 other (EBS volumes and the snapshot) | $0.20 |
| KMS (includes a key not created by this project) | $0.06 |
| VPC (public IPv4 addresses) | $0.04 |
| S3 (Terraform state) | < $0.01 |
| **Total before tax** | **$1.45** |

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
