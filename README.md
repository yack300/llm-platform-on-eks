# LLM Platform on EKS

[![CI](https://github.com/yack300/llm-platform-on-eks/actions/workflows/ci.yml/badge.svg)](https://github.com/yack300/llm-platform-on-eks/actions/workflows/ci.yml)

Reference platform demonstrating serving, gateway, RAG, cost observability, and
security for LLM inference workloads on Kubernetes (EKS), using the same GitOps
pattern (Terraform for AWS, Argo CD for everything in the cluster) used in production.

The GPU scales to zero: when nobody is using the model, both the vLLM pod and
the GPU node go away, and the next request wakes them up again while the
gateway falls back to an external provider.

## Results from a real EKS run

Measured on EKS 1.36 with one `g4dn.xlarge` (NVIDIA T4) serving Mistral-7B AWQ
through the LiteLLM gateway. Full numbers, method and caveats in
[docs/cost-comparison.md](docs/cost-comparison.md).

| Concurrent requests | Output tokens/s | p50 latency | Cost per 1M output tokens (on-demand / spot) |
|---|---|---|---|
| 1 | 50 | 2.54 s | $2.90 / $1.49 |
| 4 | 186 | 2.75 s | $0.79 / $0.40 |
| 8 | 342 | 2.98 s | $0.43 / $0.22 |

- **Scale-to-zero, end to end:** request → KEDA 0→1 → Cluster Autoscaler adds
  a GPU node from 0 → first local response in ~8–9 min (mostly the 8.7 GB
  image pull). Idle, vLLM is back at 0 in ~5 min and the GPU node is gone in ~18 min.
- **The whole validation session cost $1.48** (before tax): apply, benchmark,
  scale-to-zero test and teardown, ~2 hours of cluster time.
- **Time to first token ~0.3-0.4 s** warm (second run), for full answers of
  ~2.7-3.3 s.
- **Cold start cut from ~10-11 to ~5-6 minutes** (third run): GPU nodes from
  Karpenter, with the vLLM image pre-pulled on an EBS snapshot initialized at
  300 MiB/s. The same snapshot had made it *slower* (~14.5 min) with an EKS
  managed node group, which drops the initialization rate. Details in
  [docs/cost-comparison.md](docs/cost-comparison.md#third-run-2026-10-14-karpenter-image-cache-that-works).
- **RAG retrieval measured in CI** on 30 questions with known answers:
  switching to a multilingual embedding model took hit@4 from 0.70 to 0.90
  (Spanish 0.40 -> 0.80) for ~0.5 GiB more memory; CI fails the image if it
  drops below the baseline. Details in [docs/retrieval-eval.md](docs/retrieval-eval.md).
- **Same GPU, ~6.8x the throughput** going from 1 to 8 concurrent requests,
  with p50 latency only going from 2.5 s to 3 s (vLLM continuous batching).
- **Bugs that only showed up on real infrastructure**, fixed in this repo:
  KEDA never seeing the first request after a gateway restart, the NVIDIA
  device plugin crash-looping on CPU nodes, DNS blocked between node groups,
  a 20 GiB disk too small for the image, liveness probes killing the cold
  start, a rolling-update deadlock on a single GPU, and a Qdrant volume that
  would have survived the teardown (and kept billing).

## Architecture

```mermaid
flowchart LR
    client([Client]) --> litellm[LiteLLM gateway]
    litellm -->|local-mistral| vllm[vLLM<br/>Mistral-7B AWQ<br/>GPU node, 0..N]
    litellm -.->|fallback while vLLM is cold| claude[Anthropic API]
    litellm --> prom[(Prometheus)]
    vllm --> prom
    prom --> keda[KEDA]
    keda -->|replicas 0..N| vllm
    vllm -.->|pending pod| karpenter[Karpenter]
    karpenter -->|GPU node 0..N, spot or on-demand| vllm
    prom --> grafana[Grafana<br/>cost and latency]
    git[(GitHub main)] -->|Argo CD| cluster{{EKS cluster}}
```

Terraform provisions only AWS resources (EKS, the platform node group, IAM
via Pod Identity, Karpenter's IAM and interruption queue). Everything inside the cluster is deployed by Argo CD from this
repo, after CI (Checkov, Trivy, GitLeaks, kubeconform, Kyverno, Terraform
validate) passes on `main`.

## Project layers

| # | Layer | Main tool | What it demonstrates |
|---|------|------------------------|----------------|
| 1 | Serving | vLLM + KEDA | MLOps: deploying and autoscaling an LLM on K8s |
| 2 | Model Gateway | LiteLLM | AI Platform Engineering: unified multi-provider proxy |
| 3 | RAG | Qdrant + own Python service | Ingestion Job and Q&A API over the project's own docs, image signed in CI |
| 4 | Cost observability | Prometheus + Grafana + OpenCost/Kubecost | FinOps applied to AI: cost per request/token |
| 5 | Security | Trivy, Checkov, Kyverno, GitLeaks | DevSecOps: shift-left in the pipeline and in the cluster |

## Repo structure

```
llm-platform-on-eks/
├── terraform/
│   ├── bootstrap/            # One-time bootstrap: S3 bucket for remote state (native S3 locking)
│   ├── main.tf                # EKS cluster (official module) + platform node group + Karpenter IAM/queue
│   └── backend.hcl.example    # S3 backend template (copy to backend.hcl, gitignored)
├── apps/
│   └── rag/                   # RAG service (Python): ingestion + /ask API, Dockerfile, unit tests
├── k8s/
│   ├── vllm/                  # Deployment (GPU) + local-cpu-mock (Ollama) + Service + KEDA ScaledObject
│   ├── litellm/                # Gateway: Deployment + ConfigMap (real vs. local) (routing/rate limits)
│   ├── qdrant/                  # Vector DB for RAG (Helm values)
│   ├── rag/                    # RAG ingestion Job (Argo CD Sync hook) + API Deployment
│   ├── argocd/                 # Argo CD: install values, root app-of-apps, one Application per component
│   ├── storage/                # Default gp3 StorageClass (EKS ships none)
│   ├── karpenter/              # NodePools + EC2NodeClasses: GPU nodes from 0, platform overflow
│   ├── security/                # Kyverno policies + Trivy-operator config
│   └── observability/         # kube-prometheus-stack values + ServiceMonitors (cost/latency)
├── .github/workflows/ci.yml   # GitHub Actions: GitLeaks + Checkov + Trivy fs -> kubeconform, Kyverno, Terraform,
│                               # RAG image (tests, build, retrieval eval, Trivy, push + keyless cosign signing on main)
├── docs/
│   ├── cost-comparison.md      # Measured throughput, latency, cost per token and scale-to-zero timings
│   └── retrieval-eval.md       # RAG retrieval metrics + CI baseline, answer quality judged by Claude
└── scripts/
    ├── deploy-local.sh         # Fast bootstrap on kind/minikube (no GPU, Ollama mock)
    ├── bootstrap-argocd.sh     # EKS: install Argo CD, create the LiteLLM secret, apply the root app
    └── teardown-eks.sh         # EKS: delete apps and PVC volumes before terraform destroy
```

## Getting started (recommended order)

1. `scripts/deploy-local.sh` — spins up a local kind cluster, installs Prometheus/Grafana,
   Kyverno, Qdrant, a CPU inference mock (Ollama) and the RAG service (built locally) to
   test the gateway, RAG, and security policies without spending on GPU.
2. Once the logic works locally:
   a. `cd terraform/bootstrap && terraform init && terraform apply` — creates the S3
      bucket for remote state (done once).
   b. Copy the `backend_hcl` output to `terraform/backend.hcl` (see `backend.hcl.example`).
   c. `cd terraform && terraform init -backend-config=backend.hcl && terraform apply
      -var='admin_cidrs=["<your-ip>/32"]'` against real AWS, to create the EKS cluster, the
      platform node group (spot) and what Karpenter needs on the AWS side.
   d. `./scripts/bootstrap-argocd.sh` — installs Argo CD and applies the root
      app-of-apps; from then on Argo CD deploys everything else from this repo
      (`k8s/argocd/apps/`), in order, excluding the `*-local*` files.
3. **Important:** when you're done, run `./scripts/teardown-eks.sh` (deletes the
   apps, Karpenter's nodes and the EBS volumes behind PVCs, which `terraform destroy`
   would orphan), then `terraform destroy` in `terraform/`. Karpenter removes idle GPU
   nodes, but the EKS control plane and the platform nodes are billed hourly while they
   exist.

## Current status

Validated end to end on a real AWS account (2026-10-06): `terraform apply`,
Argo CD sync of every component, scale-to-zero and wake-up through the
Cluster Autoscaler, benchmark, and teardown with no resources left behind.

Fourth run (2026-10-20): the RAG service on EKS, with its image signed in CI
and admitted by Kyverno in Enforce only with that signature (pinned to the
verified digest); answers come from Mistral-7B through LiteLLM.

Third run (2026-10-14): GPU nodes moved from a managed node group + Cluster
Autoscaler to Karpenter, validated on EKS: spot with automatic on-demand
fallback, image cache with a provisioned initialization rate (cold start
~5-6 min), GPU node removed ~12 min after the last request.

Known gaps:
- Grafana's admin password is generated by the chart and doesn't survive an
  Argo CD re-render; it should come from a Secret created at bootstrap.
- A cold start still takes ~5-6 minutes with the image cache, mostly vLLM's
  own startup (torch.compile, CUDA graph capture), the next target; and the
  snapshot has to be rebuilt whenever the vLLM image tag changes.
- The platform NodePool's AZ-aware launches (for Qdrant/Grafana volumes
  stranded by a spot interruption) haven't been exercised on EKS yet.
- The `kyverno` Application shows OutOfSync on its policy CRDs.
- RAG answer quality is graded by Claude (docs/retrieval-eval.md), but not
  yet on Mistral's answers: that needs an EKS session, as does the
  embeddinggemma model (0.2.0+). There's no similarity threshold for
  off-topic questions yet.
- LiteLLM doesn't restart on a ConfigMap change (no config checksum on its
  Deployment); a failing ingestion Sync hook blocks newer revisions until the
  sync is terminated.
- OpenCost (infra-side cost) isn't installed yet.
