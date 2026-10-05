# LLM Platform on EKS

Reference platform demonstrating serving, gateway, RAG, cost observability, and
security for LLM inference workloads on Kubernetes (EKS), using the same GitOps
pattern (Terraform for AWS, Argo CD for everything in the cluster) used in production.

## Project layers

| # | Layer | Main tool | What it demonstrates |
|---|------|------------------------|----------------|
| 1 | Serving | vLLM + KEDA | MLOps: deploying and autoscaling an LLM on K8s |
| 2 | Model Gateway | LiteLLM | AI Platform Engineering: unified multi-provider proxy |
| 3 | RAG | Qdrant | End-to-end retrieval-augmented generation pipeline |
| 4 | Cost observability | Prometheus + Grafana + OpenCost/Kubecost | FinOps applied to AI: cost per request/token |
| 5 | Security | Trivy, Checkov, Kyverno, GitLeaks | DevSecOps: shift-left in the pipeline and in the cluster |

## Repo structure

```
llm-platform-on-eks/
├── terraform/
│   ├── bootstrap/            # One-time bootstrap: S3 bucket for remote state (native S3 locking)
│   ├── modules/
│   │   └── eks-gpu-nodegroup/  # GPU node group: IAM role, taint, spot capacity_type
│   ├── main.tf                # EKS cluster (official module) + base node group + GPU node group
│   └── backend.hcl.example    # S3 backend template (copy to backend.hcl, gitignored)
├── k8s/
│   ├── vllm/                  # Deployment (GPU) + local-cpu-mock (Ollama) + Service + KEDA ScaledObject
│   ├── litellm/                # Gateway: Deployment + ConfigMap (real vs. local) (routing/rate limits)
│   ├── qdrant/                  # Vector DB for RAG (Helm values)
│   ├── argocd/                 # Argo CD: install values, root app-of-apps, one Application per component
│   ├── storage/                # Default gp3 StorageClass (EKS ships none)
│   ├── gpu/                     # NVIDIA device plugin DaemonSet
│   ├── security/                # Kyverno policies + Trivy-operator config
│   └── observability/         # kube-prometheus-stack values + ServiceMonitors (cost/latency)
├── .github/workflows/ci.yml   # GitHub Actions: GitLeaks + Checkov + Trivy fs -> kubeconform, Kyverno, Terraform
│                               # (no build/push: no custom image, Argo CD syncs main directly)
├── docs/
│   └── cost-comparison.md      # Cost-per-1,000-requests table (local vs. external provider)
└── scripts/
    ├── deploy-local.sh         # Fast bootstrap on kind/minikube (no GPU, Ollama mock)
    ├── bootstrap-argocd.sh     # EKS: install Argo CD, create the LiteLLM secret, apply the root app
    └── teardown-eks.sh         # EKS: delete apps and PVC volumes before terraform destroy
```

## Getting started (recommended order)

1. `scripts/deploy-local.sh` — spins up a local kind cluster, installs Prometheus/Grafana,
   Kyverno, Qdrant, and a CPU inference mock (Ollama) to test the gateway, RAG, and
   security policies without spending on GPU.
2. Once the logic works locally:
   a. `cd terraform/bootstrap && terraform init && terraform apply` — creates the S3
      bucket for remote state (done once).
   b. Copy the `backend_hcl` output to `terraform/backend.hcl` (see `backend.hcl.example`).
   c. `cd terraform && terraform init -backend-config=backend.hcl && terraform apply
      -var='admin_cidrs=["<your-ip>/32"]'` against real AWS, to create the EKS cluster and
      the node groups (spot instances).
   d. `./scripts/bootstrap-argocd.sh` — installs Argo CD and applies the root
      app-of-apps; from then on Argo CD deploys everything else from this repo
      (`k8s/argocd/apps/`), in order, excluding the `*-local*` files.
3. **Important:** when you're done, run `./scripts/teardown-eks.sh` (deletes the
   apps and the EBS volumes behind PVCs, which `terraform destroy` would orphan),
   then `terraform destroy` in `terraform/`. The Cluster Autoscaler removes idle GPU nodes,
   but the EKS control plane and the platform nodes are billed hourly while they exist.

## Current status

All layers have complete manifests/config with no pending placeholders (HF model
fixed, Terraform state backend resolved, observability on Prometheus/Grafana,
GitLeaks in the pipeline, all 5 Kyverno policies written, image tags pinned).
Still pending real-world validation:
- Running `terraform apply` against a real AWS account (`terraform plan` already
  verified against one: uses the default VPC; `admin_cidrs` is a required input).
- Filling in `docs/cost-comparison.md` with real traffic data from the Grafana
  dashboard (`k8s/observability/grafana-dashboard-llm-cost.yaml`, already
  validated locally against the Ollama mock).
- `require-image-signature.yaml` ships in `Audit` mode with placeholder
  registry/key values — only relevant once a custom-built image exists (see
  `k8s/security/kyverno-policies/require-image-signature.yaml`).
