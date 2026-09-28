#!/usr/bin/env bash
# Local project bootstrap on kind, no GPU (CPU mode for vLLM).
# Goal: validate the gateway, RAG, and CI/CD logic without spending on AWS.
set -euo pipefail

# Manifest paths below are relative (../k8s/...), so always run from this
# script's directory regardless of where it was invoked from.
cd "$(dirname "$0")"

CLUSTER_NAME="llm-platform-local"

echo "==> Creating kind cluster (if it doesn't exist)..."
if ! kind get clusters | grep -q "$CLUSTER_NAME"; then
  kind create cluster --name "$CLUSTER_NAME"
else
  echo "Cluster $CLUSTER_NAME already exists, skipping creation."
fi

echo "==> Creating llm-platform namespace..."
kubectl create namespace llm-platform --dry-run=client -o yaml | kubectl apply -f -

echo "==> Installing KEDA..."
helm repo add kedacore https://kedacore.github.io/charts --force-update
helm upgrade --install keda kedacore/keda --namespace keda --create-namespace

echo "==> Installing Kyverno..."
helm repo add kyverno https://kyverno.github.io/kyverno --force-update
helm upgrade --install kyverno kyverno/kyverno --namespace kyverno --create-namespace

echo "==> Installing kube-prometheus-stack (Prometheus + Grafana)..."
# Required both by the KEDA trigger (request queue) and by the cost
# observability layer (Phase 4). See k8s/observability/.
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace -f ../k8s/observability/prometheus-grafana-values.yaml

echo "==> Installing Qdrant..."
helm repo add qdrant https://qdrant.github.io/qdrant-helm --force-update
helm upgrade --install qdrant qdrant/qdrant -n llm-platform -f ../k8s/qdrant/helm-values.yaml

echo "==> Applying security policies..."
kubectl apply -f ../k8s/security/kyverno-policies/

echo "==> Applying ServiceMonitors..."
kubectl apply -f ../k8s/observability/servicemonitors.yaml

# Local CPU mode: vllm/deployment.yaml requires a real GPU (not available in
# kind/minikube), so locally we use a mock with real CPU inference (Ollama +
# tiny model) and a variant of the LiteLLM config that points to that mock
# instead of the vLLM Service. See comments in
# k8s/vllm/local-cpu-mock-deployment.yaml and k8s/litellm/configmap-local.yaml.
echo "==> Applying local inference mock (Ollama, no GPU)..."
kubectl apply -f ../k8s/vllm/local-cpu-mock-deployment.yaml

echo "==> Creating LiteLLM secret (if it doesn't exist)..."
# k8s/litellm/deployment.yaml loads litellm-secrets via envFrom, and the config
# requires LITELLM_MASTER_KEY. Generated locally so no key is ever committed;
# an existing secret is left untouched so re-runs keep the same key.
if ! kubectl -n llm-platform get secret litellm-secrets >/dev/null 2>&1; then
  kubectl -n llm-platform create secret generic litellm-secrets \
    --from-literal=LITELLM_MASTER_KEY="sk-local-$(openssl rand -hex 16)"
else
  echo "Secret litellm-secrets already exists, skipping creation."
fi

echo "==> Applying LiteLLM (local config)..."
# ConfigMap first so pods don't start before their config exists.
kubectl apply -f ../k8s/litellm/configmap-local.yaml
kubectl apply -f ../k8s/litellm/deployment.yaml

echo "----------------------------------------------------------------------"
echo "LiteLLM master key:"
echo "  kubectl -n llm-platform get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d"
echo "Local setup ready: vllm/deployment.yaml (real GPU) and litellm/configmap.yaml"
echo "(real vLLM) were NOT applied here — those are for terraform/ against real EKS."
echo "To test against the real model, repeat this bootstrap on an EKS cluster"
echo "with a GPU node group and apply those two files instead."
echo "----------------------------------------------------------------------"
