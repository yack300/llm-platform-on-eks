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

echo "==> Creating the Grafana admin secret (if it doesn't exist)..."
# Referenced by grafana.admin.existingSecret in prometheus-grafana-values.yaml.
# Created once and kept, so Grafana's password doesn't change on every Argo CD
# sync (the chart's generated one did).
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
if ! kubectl -n monitoring get secret grafana-admin >/dev/null 2>&1; then
  kubectl -n monitoring create secret generic grafana-admin \
    --from-literal=admin-user=admin --from-literal=admin-password="$(openssl rand -hex 16)"
else
  echo "    Secret grafana-admin already exists, skipping creation."
fi

echo "==> Pointing MLflow and vLLM at the artifacts bucket..."
# k8s/training/mlflow.yaml reads its artifact store from this ConfigMap: the
# bucket name (from terraform/bootstrap) is per account, so it isn't in Git.
# MLflow reaches it through Pod Identity (terraform apply with
# -var artifacts_bucket_name=...).
ARTIFACTS_BUCKET="${ARTIFACTS_BUCKET:-$(terraform -chdir=../terraform/bootstrap output -raw artifacts_bucket_name 2>/dev/null || true)}"
kubectl create namespace training --dry-run=client -o yaml | kubectl apply -f -
if [ -n "$ARTIFACTS_BUCKET" ]; then
  kubectl -n training create configmap mlflow-storage \
    --from-literal=artifacts-destination="s3://${ARTIFACTS_BUCKET}/mlflow" \
    --dry-run=client -o yaml | kubectl apply -f -
  # vLLM's init container downloads the LoRA adapters it serves from here.
  kubectl -n llm-platform create configmap artifacts-bucket \
    --from-literal=bucket="$ARTIFACTS_BUCKET" --dry-run=client -o yaml | kubectl apply -f -
else
  echo "    No artifacts bucket (apply terraform/bootstrap, or set ARTIFACTS_BUCKET):"
  echo "    MLflow and vLLM won't start until the mlflow-storage and"
  echo "    artifacts-bucket ConfigMaps exist."
fi

echo "==> Applying the root Application (app of apps)..."
kubectl apply -f ../k8s/argocd/root.yaml

echo "----------------------------------------------------------------------"
echo "Argo CD is now syncing the platform from Git (several minutes)."
echo "Watch it:  kubectl -n argocd get applications -w"
echo "UI:        kubectl -n argocd port-forward svc/argocd-server 8080:443"
echo "           https://localhost:8080  user: admin  password:"
echo "           kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo"
echo "Grafana:   kubectl -n monitoring port-forward svc/prometheus-grafana 3000:80"
echo "           http://localhost:3000  user: admin  password:"
echo "           kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d; echo"
echo "LiteLLM master key:"
echo "           kubectl -n llm-platform get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d; echo"
echo "Before terraform destroy, run ./scripts/teardown-eks.sh (deletes PVCs/EBS volumes)."
echo "----------------------------------------------------------------------"
