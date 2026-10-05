# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Reference/portfolio platform demonstrating LLM inference serving, gateway, RAG, cost
observability, and security on EKS, using the same GitOps pattern (Terraform + FluxCD)
used in production elsewhere. It is infrastructure/config only — there is no
application code and no Dockerfile; every workload is an upstream OSS image
(`vllm/vllm-openai`, `ghcr.io/berriai/litellm`, `qdrant/qdrant`, `ollama/ollama`)
deployed via Terraform (AWS resources) and Kubernetes manifests / Helm values
(cluster workloads, reconciled by FluxCD — never `kubectl apply` by hand).

All 5 layers are meant to work together but are logically independent — see the table
in [README.md](README.md) (Serving/vLLM+KEDA, Gateway/LiteLLM, RAG/Qdrant, Cost
observability/Prometheus+Grafana+OpenCost, Security/Trivy+Checkov+Kyverno+GitLeaks).

## Commands

There is no application build/test suite (no app code). The commands that matter are
infra bootstrap and CI validation:

```bash
# Local dev cluster (kind), no GPU required — installs KEDA, Kyverno,
# kube-prometheus-stack, Qdrant, and an Ollama CPU mock instead of real vLLM
./scripts/deploy-local.sh

# Terraform remote state — run ONCE per AWS account before the main terraform/ apply
cd terraform/bootstrap && terraform init && terraform apply
# copy the `backend_hcl` output into terraform/backend.hcl (see backend.hcl.example)

# Main infra (EKS cluster + GPU node group) — real AWS, real cost
cd terraform && terraform init -backend-config=backend.hcl && terraform apply

# What CI runs (.github/workflows/ci.yml), with the same pinned images, useful to
# replicate locally before pushing (image versions live in the workflow's env:)
docker run --rm -v "$PWD":/repo -w /repo bridgecrew/checkov:3.3.20 -d terraform/ --compact --quiet
docker run --rm -v "$PWD":/repo -w /repo aquasec/trivy:0.74.0 fs --severity CRITICAL,HIGH --exit-code 1 .
docker run --rm -v "$PWD":/repo zricethezav/gitleaks:v8.30.1 git /repo --redact --verbose
docker run --rm -v "$PWD":/repo -w /repo ghcr.io/yannh/kubeconform:v0.8.0 -summary -strict -ignore-missing-schemas -ignore-filename-pattern 'values(-local)?\.yaml$' -kubernetes-version 1.36.0 k8s/
docker run --rm -v "$PWD":/repo -w /repo ghcr.io/kyverno/kyverno-cli:v1.19.1 apply k8s/security/kyverno-policies/ --resource k8s/vllm/ --resource k8s/litellm/ --resource k8s/gpu/
# terraform: fmt -check -recursive, then init -backend=false + validate in terraform/
# and init + validate in terraform/bootstrap (see the workflow's terraform job)

# Validate a single manifest instead of the whole tree
kubeconform -strict -ignore-missing-schemas -kubernetes-version 1.36.0 k8s/vllm/deployment.yaml
```

`-ignore-missing-schemas` is required because the CRDs used here (ServiceMonitor,
ScaledObject, Kyverno ClusterPolicy) have no public schema, so kubeconform silently
skips them — a typo in those files will not be caught by CI.

Checkov exceptions are deliberate and documented inline as `#checkov:skip=ID:reason`
(mostly controls that add recurring AWS cost, like KMS keys or cross-region
replication, on the tiny state bucket). Add new skips the same way, with a reason,
rather than loosening the CI command. Trivy runs its default `fs` scanners
(vulnerabilities and secrets) only; enabling `--scanners misconfig` currently
reports ~10 K8s hardening findings (e.g. readOnlyRootFilesystem) that aren't fixed.

`kyverno apply` runs the same `Enforce` admission policies the cluster uses against
the raw manifests. Any new Deployment in `llm-platform` must pin its image tag and set
`securityContext.allowPrivilegeEscalation: false` plus CPU/memory requests and limits,
or it will be rejected both in CI and at admission time.

## Architecture notes that span multiple files

**Terraform vs. Flux split is load-bearing.** Terraform only ever provisions AWS
resources (EKS cluster/node groups, IAM, S3 for state). Everything that runs
*inside* the cluster — including cluster add-ons like the NVIDIA device plugin
(`k8s/gpu/nvidia-device-plugin.yaml`) — is a plain Kubernetes manifest meant to be
picked up by FluxCD, not a Terraform `helm_release`/`kubernetes_*` resource. When
adding a new workload, follow this split rather than mixing the two. Note that the
Flux objects themselves (`GitRepository`, `Kustomization`, `HelmRelease`) are **not**
in this repo — Flux bootstrap lives outside it, and Helm-installed components
(KEDA, Kyverno, kube-prometheus-stack, Qdrant) are only expressed here as values
files plus the `helm upgrade --install` calls in `scripts/deploy-local.sh`.

**Two parallel LiteLLM/vLLM configs — real GPU vs. local mock.** Because vLLM requires
a real GPU, there are two versions of the inference backend that LiteLLM points to,
switched by which files you apply:
- Real: `k8s/vllm/deployment.yaml` (GPU, taints tolerated, model
  `TheBloke/Mistral-7B-Instruct-v0.2-AWQ` served as `local-mistral`) +
  `k8s/litellm/configmap.yaml`.
- Local/no-GPU: `k8s/vllm/local-cpu-mock-deployment.yaml` (Ollama running
  `qwen2.5:0.5b`, OpenAI-compatible, real but tiny inference) +
  `k8s/litellm/configmap-local.yaml`.
Both configmaps use the same `litellm-config` ConfigMap name and the same
`model_name: local-mistral`, so the client-facing API is identical regardless of
which backend is deployed — `scripts/deploy-local.sh` applies the local pair,
production applies the GPU pair. Don't let these two drift apart on
`litellm_settings`/`router_settings` without a reason. The one intentional
difference: only the production config defines the `claude-fallback` model and
`fallbacks`, since there's no Anthropic key locally.

**Terraform remote state has its own bootstrap module.** `terraform/bootstrap/`
creates the S3 bucket (native S3 locking via `use_lockfile`, no DynamoDB) for
`terraform/`'s own state, and is
applied once, standalone, with local state (chicken-and-egg: you can't use a bucket
as backend for the config that creates it). `terraform/main.tf` uses `backend "s3" {}`
(empty — partial config) resolved via `-backend-config=backend.hcl`, which is
gitignored and generated from `terraform/bootstrap`'s `backend_hcl` output.

**Autoscaling and observability share one Prometheus install.** KEDA's
`ScaledObject` (`k8s/vllm/keda-scaledobject.yaml`) queries the same
kube-prometheus-stack Prometheus (`k8s/observability/prometheus-grafana-values.yaml`,
release name `prometheus`) that scrapes cost/latency metrics via the
`ServiceMonitor`s in `k8s/observability/servicemonitors.yaml`. vLLM exposes native
Prometheus metrics; LiteLLM needs `litellm_settings.success_callback`/
`failure_callback: ["prometheus"]` set to expose its `/metrics` endpoint — this is
what feeds `docs/cost-comparison.md`. OpenCost (infra-side cost) is not installed
anywhere yet — it only appears as a documented next step in the comments at the end
of `prometheus-grafana-values.yaml`.

**Scale-to-zero spans three layers.** The ScaledObject has two triggers:
`gateway-demand` reads `litellm_deployment_total_requests_total{requested_model="local-mistral"}`
from LiteLLM (always running) to wake vLLM from 0, and `vllm-queue` reads
`vllm:num_requests_waiting` (colon in the name) to scale out once vLLM is up. A
vLLM-only trigger can never wake it: at 0 replicas nothing publishes the metric.
Use the deployment-level LiteLLM metric, not `litellm_proxy_total_requests_metric`,
which doesn't count requests that failed because the backend was down. While vLLM
cold-starts, LiteLLM's `fallbacks` route to `claude-fallback`. On EKS, waking from 0
also needs a GPU node: Terraform gives the Cluster Autoscaler its IAM role (EKS Pod
Identity, service account `kube-system/cluster-autoscaler`) and tags the GPU ASG with
`node-template` resources/taints so it can scale that group up from 0. The
autoscaler itself is deployed in-cluster (by Argo CD); until then the vLLM pod stays
Pending. `keda-scaledobject-local.yaml`
mirrors the triggers against the Ollama mock (tested locally: scales to 0 when idle,
wakes on the next request).

**CI has no build/deploy stage on purpose.** `.github/workflows/ci.yml` runs scans
(Checkov, Trivy fs, GitLeaks) and, once they pass, validation (kubeconform and
`kyverno apply` against `k8s/`, `terraform validate`/`fmt` against `terraform/` and
`terraform/bootstrap/`) — it gates what's allowed to reach `main`, then FluxCD reconciles directly from `main`. If real
application code is ever added to this repo (e.g. the RAG ingestion pipeline, which
today explicitly lives outside this repo per the comment in
`k8s/qdrant/helm-values.yaml`), build/push stages would need to come back.

**Cost control is a running theme, not just a comment.** `gpu_nodes_desired` defaults
to `0` (`terraform/variables.tf`; afterwards the Cluster Autoscaler owns the count and
Terraform ignores it), GPU and platform node groups use spot capacity, the default VPC
avoids a NAT Gateway,
and KEDA's `minReplicaCount: 0` scales vLLM to zero when idle. Don't change these
defaults without a reason — the whole point of the project is demonstrating
cost-aware AI infra.

## Repo-wide conventions

- All comments/docs/README content must be written in **English**, even when
  discussing the repo in another language in chat.
- `k8s/*-local*` files (`local-cpu-mock-deployment.yaml`, `configmap-local.yaml`,
  `prometheus-grafana-values-local.yaml`, `keda-scaledobject-local.yaml`) are strictly for `scripts/deploy-local.sh` /
  kind-minikube and must never be applied against real EKS.
- The local profile is sized for an 8 GB laptop (Docker Desktop at ~6 GB): LiteLLM is
  scaled to 1 replica, Alertmanager/node-exporter and Kyverno's reports/cleanup
  controllers are disabled. The full stack at production settings exhausts that
  memory and takes down the kind control plane, so keep local overrides in the
  script or `*-local*` files rather than trimming the production manifests.
- `terraform/` uses the account's default VPC unless both `vpc_id` and `subnet_ids`
  are set. `admin_cidrs` (who can reach the public EKS API) has no default on
  purpose and is required for any `plan`/`apply`, e.g.
  `-var='admin_cidrs=["<ip>/32"]'`. Keep `kubernetes_version` in EKS *standard*
  support: extended support bills a much higher hourly rate, and retired versions
  can't be created (1.30 already can't).
