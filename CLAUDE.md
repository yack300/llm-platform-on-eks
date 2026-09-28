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
cd scripts && ./deploy-local.sh

# Terraform remote state — run ONCE per AWS account before the main terraform/ apply
cd terraform/bootstrap && terraform init && terraform apply
# copy the `backend_hcl` output into terraform/backend.hcl (see backend.hcl.example)

# Main infra (EKS cluster + GPU node group) — real AWS, real cost
cd terraform && terraform init -backend-config=backend.hcl && terraform apply

# What CI runs (.gitlab-ci.yml), useful to replicate locally before pushing:
checkov -d terraform/ --compact --quiet
trivy fs --severity CRITICAL,HIGH --exit-code 1 .
gitleaks detect --source . --verbose --redact
kubeconform -summary -strict -ignore-missing-schemas -kubernetes-version 1.30.0 k8s/
terraform -chdir=terraform fmt -check -recursive && terraform -chdir=terraform validate
terraform -chdir=terraform/bootstrap validate
```

## Architecture notes that span multiple files

**Terraform vs. Flux split is load-bearing.** Terraform only ever provisions AWS
resources (EKS cluster/node groups, IAM, S3/DynamoDB for state). Everything that runs
*inside* the cluster — including cluster add-ons like the NVIDIA device plugin
(`k8s/gpu/nvidia-device-plugin.yaml`) — is a plain Kubernetes manifest meant to be
picked up by FluxCD, not a Terraform `helm_release`/`kubernetes_*` resource. When
adding a new workload, follow this split rather than mixing the two.

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
`litellm_settings`/`router_settings` without a reason.

**Terraform remote state has its own bootstrap module.** `terraform/bootstrap/`
creates the S3 bucket + DynamoDB lock table for `terraform/`'s own state, and is
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
what feeds `docs/cost-comparison.md`.

**CI has no build/deploy stage on purpose.** `.gitlab-ci.yml` only has `scan-iac`
(Checkov, Trivy fs, GitLeaks) and `validate` (kubeconform against `k8s/`, `terraform
validate`/`fmt` against `terraform/` and `terraform/bootstrap/`) — it gates what's
allowed to reach `main`, then FluxCD reconciles directly from `main`. If real
application code is ever added to this repo (e.g. the RAG ingestion pipeline, which
today explicitly lives outside this repo per the comment in
`k8s/qdrant/helm-values.yaml`), build/push stages would need to come back.

**Cost control is a running theme, not just a comment.** `gpu_nodes_desired` defaults
to `0` (`terraform/variables.tf`), the GPU node group uses spot capacity by default,
and KEDA's `minReplicaCount: 0` scales vLLM to zero when idle. Don't change these
defaults without a reason — the whole point of the project is demonstrating
cost-aware AI infra.

## Repo-wide conventions

- All comments/docs/README content must be written in **English**, even when
  discussing the repo in another language in chat.
- `k8s/*-local*` files (`local-cpu-mock-deployment.yaml`, `configmap-local.yaml`) are
  strictly for `scripts/deploy-local.sh` / kind-minikube and must never be applied
  against real EKS.
- `vpc_id` and `subnet_ids` (`terraform/variables.tf`) have no defaults — they're
  required inputs for any real `terraform apply`, since the module doesn't create a VPC.
