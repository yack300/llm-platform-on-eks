# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Reference/portfolio platform demonstrating LLM inference serving, gateway, RAG, cost
observability, and security on EKS, using the same GitOps pattern (Terraform + Argo CD)
used in production elsewhere. Mostly infrastructure/config: every workload is an
upstream OSS image (`vllm/vllm-openai`, `ghcr.io/berriai/litellm`, `qdrant/qdrant`,
`ollama/ollama`) except the RAG service in `apps/rag/` (Python, the only image built
here), deployed via Terraform (AWS resources) and Kubernetes manifests / Helm values
(cluster workloads, synced by Argo CD — never `kubectl apply` by hand on EKS).

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

# Main infra (EKS cluster, platform node group, Karpenter IAM/queue) — real AWS, real cost
cd terraform && terraform init -backend-config=backend.hcl && terraform apply -var='admin_cidrs=["<ip>/32"]'
# plus -var="artifacts_bucket_name=$(terraform -chdir=bootstrap output -raw artifacts_bucket_name)":
# Pod Identity to the bootstrap's artifacts bucket for MLflow and for vLLM, which
# downloads the LoRA adapters it serves from it (without it vLLM doesn't start)

# Optional, experimental: EBS snapshot with the vLLM image pre-pulled for the GPU nodes'
# data volume. Build it any time; point the gpu EC2NodeClass at it (or back to `off`)
# once the cluster is up. Not managed by Terraform: delete it when done.
./scripts/build-gpu-image-cache.sh
./scripts/set-gpu-image-cache.sh [snap-...|off]

# Hand the cluster to Argo CD (installs it, creates the LiteLLM secret, applies root.yaml)
./scripts/bootstrap-argocd.sh            # ANTHROPIC_API_KEY=... optional, for claude-fallback

# Measurements (stdlib Python, need: kubectl -n llm-platform port-forward svc/litellm-gateway 4000:4000)
./scripts/measure-cold-start.py          # from vLLM + GPU nodes at 0 to the first vLLM answer
./scripts/benchmark.py --json out.json   # TTFT, latency, tokens/s at concurrency 1,4,8
./scripts/gpu-interference.sh            # the same with a job in the GPU's other time-sliced share

# Teardown: ALWAYS before terraform destroy (removes Karpenter's nodes and PVC-backed EBS volumes)
./scripts/teardown-eks.sh

# What CI runs (.github/workflows/ci.yml), with the same pinned images, useful to
# replicate locally before pushing (image versions live in the workflow's env:)
docker run --rm -v "$PWD":/repo -w /repo bridgecrew/checkov:3.3.20 -d terraform/ --compact --quiet
docker run --rm -v "$PWD":/repo -w /repo aquasec/trivy:0.74.0 fs --severity CRITICAL,HIGH --exit-code 1 .
docker run --rm -v "$PWD":/repo zricethezav/gitleaks:v8.30.1 git /repo --redact --verbose
docker run --rm -v "$PWD":/repo -w /repo ghcr.io/yannh/kubeconform:v0.8.0 -summary -strict -schema-location default -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/4c8dc296d32b06d15ccde9668ff136c951f4d539/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' -ignore-filename-pattern 'values(-local)?\.yaml$' -kubernetes-version 1.36.0 k8s/
# kyverno: one -r per workload file (see the workflow's kyverno job for why), without
# require-image-signature, e.g.
docker run --rm -v "$PWD":/repo -w /repo ghcr.io/kyverno/kyverno-cli:v1.19.1 apply k8s/security/kyverno-policies/disallow-latest-tag.yaml k8s/security/kyverno-policies/disallow-privileged-containers.yaml k8s/security/kyverno-policies/require-resource-limits.yaml k8s/security/kyverno-policies/restrict-host-network.yaml -r k8s/vllm/deployment.yaml -r k8s/vllm/local-cpu-mock-deployment.yaml -r k8s/litellm/deployment.yaml -r k8s/rag/api.yaml -r k8s/rag/ingest-job.yaml -r k8s/training/mlflow.yaml
# RAG unit tests (no model or Qdrant needed) and image build (from the repo root)
docker run --rm -v "$PWD/apps/rag":/app -w /app python:3.12.16-slim sh -c 'pip install -q -r requirements-dev.txt && python -m pytest -q tests'
docker build -f apps/rag/Dockerfile -t llm-platform-rag:dev .
# Retrieval evaluation of that image (CI runs it with the baseline; docs/retrieval-eval.md)
docker run --rm -v "$PWD/apps/rag/eval":/eval:ro llm-platform-rag:dev python -m rag.evaluate --questions /eval/questions.yaml --baseline /eval/baseline.json
# Answer quality, judged by Claude (needs ANTHROPIC_API_KEY; costs cents). Answers come
# from a cluster: kubectl -n llm-platform exec -i deploy/rag-api -- python -m rag.answers
#   < apps/rag/eval/questions.yaml > apps/rag/eval/answers/<date>.json
# ANSWERS=/eval/judge/calibration.json checks the judge itself against known verdicts.
docker run --rm -v "$PWD/apps/rag/eval":/eval -w /eval/judge -e ANSWERS=/eval/answers/<date>.json -e ANTHROPIC_API_KEY -e HOME=/tmp -e PROMPTFOO_DISABLE_TELEMETRY=1 --entrypoint promptfoo ghcr.io/promptfoo/promptfoo:0.125.1 eval --no-cache -o /eval/judge/results.json
python3 apps/rag/eval/judge/summary.py apps/rag/eval/judge/results.json
# Promotion gate for a LoRA adapter: the same questions answered with
# `rag.answers --model <adapter>` and with the base model, each judged, then
python3 apps/rag/eval/judge/promote.py --base <base-results.json> --candidate <candidate-results.json>
# Fine-tuning: unit tests run inside the CPU image (they need torch); the
# pipeline smoke test runs Argo Workflows + MLflow + a tiny model on its own
# kind cluster (llm-finetune-local)
docker build -f apps/finetune/Dockerfile -t llm-platform-finetune:dev .
docker run --rm --user 0 -v "$PWD/apps/finetune":/src -w /src llm-platform-finetune:dev sh -c 'pip install -q -r requirements-dev.txt && python -m pytest -q -p no:cacheprovider tests'
./scripts/finetune-local.sh
# Kueue's GPU queues on their own kind cluster (llm-teams-local), fake GPUs:
# a research job borrows product's share, product takes it back (docs/gpu-teams.md)
./scripts/teams-local.sh
# OpenCost on that cluster, with a scenario whose cost is known (MINUTES)
./scripts/chargeback-local.sh
python3 -m unittest discover -s tests   # the report's arithmetic (CI runs it)
# GPU cost per team (needs port-forwards of opencost 9003 and prometheus 9090)
./scripts/gpu-chargeback.py --window 6h
# Kyverno policy tests (pods that must pass or be denied; CI runs them too)
docker run --rm -v "$PWD":/repo -w /repo ghcr.io/kyverno/kyverno-cli:v1.19.1 test tests/kyverno/
# terraform: fmt -check -recursive, then init -backend=false + validate in terraform/
# and init + validate in terraform/bootstrap (see the workflow's terraform job)

# Validate a single manifest instead of the whole tree
# (single file: same docker command, with a file path instead of k8s/)
```

CRD resources (Argo CD Application, ScaledObject, ServiceMonitor, ClusterPolicy,
Karpenter NodePool/EC2NodeClass) are
validated against the community datreeio/CRDs-catalog schemas, pinned to a commit in
the workflow's `CRD_SCHEMAS`. There's deliberately no `-ignore-missing-schemas`: a new
CRD kind without a schema in that catalog fails CI instead of being skipped silently.

Checkov exceptions are deliberate and documented inline as `#checkov:skip=ID:reason`
(mostly controls that add recurring AWS cost, like KMS keys or cross-region
replication, on the tiny state bucket). Add new skips the same way, with a reason,
rather than loosening the CI command. Trivy runs its default `fs` scanners
(vulnerabilities and secrets) only; enabling `--scanners misconfig` currently
reports ~10 K8s hardening findings (e.g. readOnlyRootFilesystem) that aren't fixed.

`kyverno apply` runs the same `Enforce` admission policies the cluster uses against
the raw manifests. Any new Deployment in `llm-platform` must pin its image tag and set
`securityContext.allowPrivilegeEscalation: false` plus CPU/memory requests and limits,
or it will be rejected both in CI and at admission time. Pass workload manifests one
`-r` per file: with a directory, `kyverno apply` ignores every other `--resource`, and
the same-named EKS/local LiteLLM ConfigMaps make it panic yet exit 0 (both hid
LiteLLM from CI until the RAG work). Container names with hyphens need the quoted
JMESPath form in `disallow-latest-tag.yaml`.

## Architecture notes that span multiple files

**Terraform vs. Argo CD split is load-bearing.** Terraform only ever provisions AWS
resources (EKS cluster, the fixed platform node group, add-ons, IAM/Pod Identity,
Karpenter's IAM roles and interruption queue, S3 for state). Everything that runs
*inside* the cluster — including Karpenter itself and its NodePools — is deployed by
Argo CD from this repo, not by a Terraform
`helm_release`/`kubernetes_*` resource. When adding a new workload, follow this split
rather than mixing the two. The one exception is the NVIDIA device plugin: GPU nodes run
Bottlerocket's NVIDIA variant, which ships it as a host service, so there's no
DaemonSet to deploy (and adding one would conflict with it).

**Argo CD layout (`k8s/argocd/`).** `scripts/bootstrap-argocd.sh` is the only
imperative step on EKS: it installs Argo CD (`install/argocd-values.yaml`), creates the
`litellm-secrets` Secret (never in Git: the repo is public), and applies `root.yaml`,
an app-of-apps over `apps/`. Each file in `apps/` is one `Application`: plain
directories of this repo (excluding `*local*` files), or Helm charts as multi-source
apps (chart from its repo + `$values/<path>` from this repo). Sync waves order them
(-1 StorageClass, 0 operators/add-ons, 1 things needing their CRDs, 2 vLLM/LiteLLM,
3 RAG);
waves only wait on child Applications because `argocd-values.yaml` restores the
Application health check. To add a component: add an `apps/<name>.yaml` with the right
wave; chart versions are pinned in each file. `root.yaml` and every app point at the
GitHub repo URL; change them together if the repo is renamed. `scripts/deploy-local.sh`
still installs the same components with `helm upgrade --install` for kind, without
Argo CD (memory).

**Things on EKS that only exist for EKS:** the gp3 default StorageClass
(`k8s/storage/`, EKS ships none), the EBS CSI driver and Karpenter (IAM in Terraform,
controller and `k8s/karpenter/` NodePools via Argo CD), control plane ServiceMonitors
disabled in the Prometheus values. vLLM's
Deployment has no `replicas` and its Application ignores `/spec/replicas`, because KEDA
owns the count; adding `replicas` back would make self-heal undo every scale-to-zero.
Teardown must go through `scripts/teardown-eks.sh` before `terraform destroy`:
Karpenter's instances aren't Terraform resources, so the script deletes the NodePools
first (with root's auto-sync off, or Argo CD recreates them) and waits for the nodes
to go while Karpenter still runs; destroying with PVCs still present orphans their EBS
volumes. Both keep billing.

**Karpenter (`k8s/karpenter/`).** Three NodePools: `gpu` (g4dn.xlarge on spot, whole)
and `gpu-shared` (on-demand, time-sliced through its EC2NodeClass's `userData`), both
tainted `nvidia.com/gpu`, removed 5 min after they're empty and limited to 1 GPU
(Karpenter doesn't know about the shares), and `platform`
(overflow for platform pods beyond the fixed 2-node managed node group, which runs
Karpenter itself). EC2NodeClasses find subnets and the node security group by the
`karpenter.sh/discovery` tag Terraform adds, and reference the node role by its fixed
name; nodes are Bottlerocket, pinned by AMI alias. The GPU data volume's image cache
(`snapshotID` + `volumeInitializationRate`) is set in-cluster by
`scripts/set-gpu-image-cache.sh` and ignored by Argo CD (`RespectIgnoreDifferences`),
because the snapshot is per account. Karpenter's Helm chart comes from an OCI registry
registered in `argocd-values.yaml`.

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
`litellm_settings`/`router_settings` without a reason. The intentional
differences: only the production config defines the `claude-fallback` model and
`fallbacks`, since there's no Anthropic key locally (and so production sets
`num_retries: 0`, falling back at once instead of retrying a scaled-to-zero
vLLM), and the `otel` callback, since kind runs no Tempo.

**GPU teams (Kueue, `k8s/teams/`, docs/gpu-teams.md).** Two teams share the GPUs:
product (`llm-platform`, vLLM) has the on-demand GPU, time-sliced in 2, and reclaims it
from borrowers; research (`training`) has the whole spot GPU for the fine-tuning
workflows, and a `research-shared` queue for small jobs that borrow the share product
isn't using. Training must not go to `research-shared`: time-slicing doesn't isolate
memory (vLLM is capped at `--gpu-memory-utilization 0.45` for that reason, and KEDA at
1 replica: two don't fit a g4dn.xlarge's RAM). Only pods labeled `kueue.x-k8s.io/queue-name` are queued: vLLM's Deployment
and the WorkflowTemplate's `podMetadata`; Kyverno's `require-gpu-queue` denies a GPU
pod without the label in those namespaces. Kueue's webhooks fail closed, so
`managedJobsNamespaceSelector` in `kueue-values.yaml` must keep listing only the
teams' namespaces: widening it makes pod creation there depend on Kueue being up.
Quotas count `nvidia.com/gpu` only (CPU/memory excluded) and its flavors pin pods to a
Karpenter capacity type through the node selector. An evicted workflow step
is an error to Argo ("pod deleted"): the steps' `retryStrategy: OnError` is what queues
them again, from the start (only a pod evicted while its step is still Pending is
recreated without it). Kueue's
finalizers on queued pods and Workloads are why `teardown-eks.sh` empties the queues
(vLLM's app, the workflows) before deleting root.

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
what feeds `docs/cost-comparison.md`. OpenCost (`k8s/observability/opencost-values.yaml`)
reads that Prometheus and needs its own ServiceMonitor there. Don't quote its GPU
costs: 1.121.4 bills each time-sliced share as a whole GPU, even with GPU Feature
Discovery's labels on the node (fixed after it, opencost#4173). `scripts/gpu-chargeback.py` takes only its GPU hours
and splits the nodes' price by shares held (docs/gpu-teams.md).

**Scale-to-zero spans three layers.** The ScaledObject has three triggers:
`gateway-demand` reads `litellm_deployment_total_requests_total{requested_model=~"local-mistral|rag-behavior-.*"}`
(the base model and the LoRA adapters vLLM serves) from LiteLLM (always running) to wake vLLM from 0, and `vllm-queue` reads
`vllm:num_requests_waiting` (colon in the name) to scale out once vLLM is up. A
vLLM-only trigger can never wake it: at 0 replicas nothing publishes the metric.
A third one, `startup-in-progress` (kube-state-metrics), holds the replica while its pod
isn't Ready yet, for up to 20 minutes: otherwise gateway-demand expires ~7 minutes after
the last request and KEDA kills a slower cold start mid-load. Use the deployment-level LiteLLM metric, not `litellm_proxy_total_requests_metric`,
which doesn't count requests that failed because the backend was down. The query
also can't be a plain `increase()`: LiteLLM creates the series on its first request
already at 1, so `increase()` reads 0 for the very first request after LiteLLM starts
(the query falls back to the raw value for series younger than the window). While vLLM
cold-starts, LiteLLM's `fallbacks` route to `claude-fallback`. On EKS, waking from 0
also needs a GPU node: the Pending vLLM pod makes Karpenter launch one from the `gpu`
NodePool (until Karpenter is synced by Argo CD, the pod stays Pending). A cold start takes ~10-11 minutes without the image cache (image pull + vLLM's own
startup) and ~5-6 with it (docs/cost-comparison.md, third run). The startup trigger
must keep excluding terminating pods: counting them woke vLLM after every
scale-to-zero, in a loop. `keda-scaledobject-local.yaml`
mirrors the triggers against the Ollama mock (tested locally: scales to 0 when idle,
wakes on the next request).

**CI gates main; Argo CD deploys.** `.github/workflows/ci.yml` runs scans (Checkov,
Trivy fs, GitLeaks) and, once they pass, validation (kubeconform and `kyverno apply`
against `k8s/`, `terraform validate`/`fmt`) plus the `rag-image` job: unit tests, a
check that `k8s/rag/` uses the tag in `apps/rag/VERSION`, build, retrieval evaluation
against `apps/rag/eval/baseline.json`, Trivy image scan, and
on main only, push to GHCR and keyless `cosign sign`. There's no deploy step: Argo CD
syncs from `main`. Image tags are immutable: to release RAG changes, bump
`apps/rag/VERSION` and the tags in `k8s/rag/` in the same commit (CI skips pushing a
tag that already exists). Cosign stays on v2 on purpose: v3 writes signatures only as
OCI referrers in the new bundle format, which Kyverno's `verifyImages` doesn't read and
GHCR can't serve.

**RAG (`apps/rag/`, `k8s/rag/`).** One image, two entrypoints: the ingestion Job
(`python -m rag.ingest`) indexes the project's own docs (README, CLAUDE.md, docs/,
copied into the image at build time, so the index matches the image version) and the
API (`POST /ask`) retrieves from Qdrant and answers through LiteLLM like any client.
Embeddings run on CPU with fastembed; the model is baked into the image and pods run
with `HF_HUB_OFFLINE=1`. Each ingestion writes a new collection and atomically
repoints the `platform-docs` alias, deleting older versions, so queries never see a
half-built index. On EKS the Job is an Argo CD Sync hook (Job templates are
immutable); `require-image-signature.yaml` admits `ghcr.io/yack300/*` only with a
signature from this repo's ci.yml on main. Locally, `deploy-local.sh` builds the image
as `llm-platform-rag:dev` and loads it into kind, a name that policy doesn't match.
Prompts go as a single user message: Mistral's chat template rejects a system role.
The embedding model is `google/embeddinggemma-300m` with task prefixes
(`QUERY_PREFIX`/`DOCUMENT_PREFIX`), chosen over bge-small-en with `rag.evaluate`
(docs/retrieval-eval.md): ~850 MiB resident, hence the 1.5 GiB limits in `k8s/rag/`.
Chunks are embedded with a `<file> - <heading>` prefix (`Chunk.embedding_text`); the
stored and prompted text is the plain chunk.
Changing the model, prefixes, chunking or the docs themselves changes retrieval, and CI
fails the image below `apps/rag/eval/baseline.json`; a doc edit that removes a
question's `evidence` text fails it too (update `eval/questions.yaml`). Answer quality is a separate,
manual step (`.github/workflows/judge.yml`, Claude as judge via promptfoo), not a CI gate:
it costs money and isn't deterministic. Keep `judge/calibration.json` passing when
changing the rubrics in `judge/tests.js`.
Embedding memory grows with `EMBED_BATCH` (8; 32 OOMed the Job at 768 MiB). If the
ingestion hook keeps failing, Argo CD stays on that revision until the sync operation
is terminated. LiteLLM's ConfigMap (incl. `drop_params: true`, needed because
claude-sonnet-5 rejects temperature != 1) is only read at startup: restart the
Deployment after changing it.

**Cost control is a running theme, not just a comment.** There are no GPU nodes while
vLLM is idle (Karpenter removes them once empty), NodePools cap GPUs and extra CPUs with
`limits`, GPU and platform capacity prefers spot, the default VPC avoids a NAT Gateway,
and KEDA's `minReplicaCount: 0` scales vLLM to zero when idle. Don't change these
defaults without a reason — the whole point of the project is demonstrating
cost-aware AI infra.

**Tracing (Tempo, `rag/tracing.py`).** On EKS the RAG API and LiteLLM send
OpenTelemetry spans over OTLP/HTTP to Tempo (`k8s/argocd/apps/tempo.yaml`,
single binary, no persistent volume, 24 h retention), a Grafana datasource. One
question is one trace: FastAPI's native telemetry opens the request span,
`rag.ask` and its `rag.embed`/`rag.retrieve`/`rag.generate` stages hang under it
(scores, sources, model, token counts as attributes), and LiteLLM's spans hang
under `rag.generate` because the API injects a `traceparent` into the model call.
`/healthz` and `/metrics` are excluded. Tracing is off whenever
`OTEL_EXPORTER_OTLP_ENDPOINT` is unset (unit tests, CI's evaluation, kind, where
`deploy-local.sh` strips it). LiteLLM's spans include the full prompt. The Tempo
chart lives in grafana-community/helm-charts (deprecated in grafana/helm-charts). Measured results:
docs/tracing.md.

**Fine-tuning (`apps/finetune/`, `k8s/training/`).** A
LoRA adapter for the served Mistral-7B that teaches behavior, not facts:
answer in the question's language, decline when the context lacks the
answer, cite. `data/generate.py` builds the dataset from real retrievals
(runs in the RAG image; seeds exclude chunks holding evaluation evidence or
questions); the dataset is committed and baked into the trainer image.
`finetune.train` (QLoRA on GPU, fp32 on CPU) logs to MLflow and registers
each adapter as a version of `rag-behavior-lora` with alias `candidate`;
`finetune.evaluate` scores base vs adapter with the heuristics in
`finetune/behavior.py` (no API). Argo Workflows runs both from the
`finetune` WorkflowTemplate, whose default `pod-spec-patch` asks for the GPU.
MLflow needs `--allowed-hosts` (it rejects unknown Host headers) and
`MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false` (2.4 GiB to ~370 MiB), and runs
its own image (`apps/mlflow/`: the official one lacks boto3). On EKS
(Argo CD apps `argo-workflows`, wave 0, and `training`, wave 1) it proxies
artifacts to the artifacts bucket in `terraform/bootstrap`, which outlives
the cluster, through Pod Identity; the bucket's name reaches it in the
`mlflow-storage` ConfigMap that `bootstrap-argocd.sh` creates. Its SQLite
registry is per cluster. CI publishes the trainer's GPU variant (~10 GB,
CUDA 13 like the vLLM image, checked for the T4's sm_75) only when
`apps/finetune/VERSION` is new; `push-and-sign.sh` publishes and signs all
three images. The Kyverno policies cover `training` too, so workflow pods
need Argo's executor limits and securityContext from the chart's values.
Argo Workflows' full CRDs (~5 MB each) pushed Argo CD's application
controller past 1 GiB into an OOMKill loop: keep its 2 GiB limit. MLflow's
registry is lost at teardown: export results first (`apps/finetune/results/`).
Measured results: docs/fine-tuning.md.
Serving adapters: vLLM loads them with `--enable-lora`/`--lora-modules`, from
an init container that downloads them from the artifacts bucket at every
start (runtime-loaded adapters wouldn't survive scale-to-zero; read-only Pod
Identity for the `vllm` service account, bucket name from the
`artifacts-bucket` ConfigMap). An adapter name lives in four places that must
match: the init container's `ADAPTERS` and `--lora-modules` in
`k8s/vllm/deployment.yaml`, a LiteLLM model (no fallback, on purpose) and the
RAG API's `ALLOWED_MODELS`, which `/ask`'s optional `model` field is checked
against. `rag.answers --model` answers the evaluation set with an adapter;
`judge/promote.py` decides (target metric must improve, the others may drop
by at most one check); promoting is a commit of the RAG API's `CHAT_MODEL`
plus a claude-fallback for that adapter in LiteLLM (candidates under
evaluation get none, so their answers can't come from Claude). Since the
ninth run the RAG API serves `rag-behavior-v1`; `deploy-local.sh` strips
`CHAT_MODEL` on kind, whose Ollama mock has no adapters.

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
