#!/usr/bin/env bash
# One-time bootstrap of a fresh EKS cluster (after `terraform apply` in
# terraform/): installs Argo CD, creates the one Secret that can't live in this
# public repo, and hands everything else to Git via the root "app of apps".
#
# Usage:
#   export AWS_PROFILE=<your-profile>
#   ANTHROPIC_API_KEY=... ./scripts/bootstrap-argocd.sh   # key optional
set -euo pipefail

cd "$(dirname "$0")"

CLUSTER_NAME="${CLUSTER_NAME:-llm-platform-demo}"
AWS_REGION="${AWS_REGION:-us-east-1}"
ARGOCD_CHART_VERSION="10.9.6"

echo "==> Pointing kubectl at ${CLUSTER_NAME}..."
aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$AWS_REGION"

echo "==> Installing Argo CD (chart ${ARGOCD_CHART_VERSION})..."
helm repo add argo https://argoproj.github.io/argo-helm --force-update
helm upgrade --install argocd argo/argo-cd --version "$ARGOCD_CHART_VERSION" \
  -n argocd --create-namespace -f ../k8s/argocd/install/argocd-values.yaml --wait

echo "==> Creating the LiteLLM secret (if it doesn't exist)..."
# The only manual step in the GitOps flow, on purpose: this repo is public, so
# the master key (and the optional Anthropic key used by claude-fallback) must
# never be committed. A production setup would use External Secrets or Sealed
# Secrets instead. An existing secret is left untouched so re-runs keep the key.
kubectl create namespace llm-platform --dry-run=client -o yaml | kubectl apply -f -
if ! kubectl -n llm-platform get secret litellm-secrets >/dev/null 2>&1; then
  args=(--from-literal=LITELLM_MASTER_KEY="sk-$(openssl rand -hex 24)")
  if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    args+=(--from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY")
  else
    echo "    ANTHROPIC_API_KEY not set: claude-fallback won't work while vLLM is down."
  fi
  kubectl -n llm-platform create secret generic litellm-secrets "${args[@]}"
else
  echo "    Secret litellm-secrets already exists, skipping creation."
fi

echo "==> Applying the root Application (app of apps)..."
kubectl apply -f ../k8s/argocd/root.yaml

echo "----------------------------------------------------------------------"
echo "Argo CD is now syncing the platform from Git (several minutes)."
echo "Watch it:  kubectl -n argocd get applications -w"
echo "UI:        kubectl -n argocd port-forward svc/argocd-server 8080:443"
echo "           https://localhost:8080  user: admin  password:"
echo "           kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo"
echo "LiteLLM master key:"
echo "           kubectl -n llm-platform get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d; echo"
echo "Before terraform destroy, run ./scripts/teardown-eks.sh (deletes PVCs/EBS volumes)."
echo "----------------------------------------------------------------------"
