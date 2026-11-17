#!/usr/bin/env bash
# Smoke test of the fine-tuning pipeline on kind, no GPU: Argo Workflows,
# MLflow and the trainer image (CPU variant), running the same workflow
# template as EKS with a tiny random Mistral-architecture model. It checks
# the plumbing (data, LoRA on Mistral's modules, MLflow logging and registry,
# passing the version between steps), not the quality: that model's answers
# are noise.
#
# A cluster of its own, separate from deploy-local.sh's: the full platform
# leaves no room for training on an 8 GB laptop. Delete it when done:
#   kind delete cluster --name llm-finetune-local
set -euo pipefail

cd "$(dirname "$0")"

CLUSTER_NAME="llm-finetune-local"
ARGO_WORKFLOWS_CHART_VERSION="2.0.18"
IMAGE="llm-platform-finetune:dev"
MLFLOW_IMAGE="llm-platform-mlflow:dev"

echo "==> Creating kind cluster (if it doesn't exist)..."
if ! kind get clusters | grep -qx "$CLUSTER_NAME"; then
  kind create cluster --name "$CLUSTER_NAME"
else
  echo "Cluster $CLUSTER_NAME already exists, skipping creation."
fi
kubectl config use-context "kind-$CLUSTER_NAME"
kubectl create namespace training --dry-run=client -o yaml | kubectl apply -f -

echo "==> Installing Argo Workflows..."
helm upgrade --install argo-workflows argo-workflows \
  --repo https://argoproj.github.io/argo-helm --version "$ARGO_WORKFLOWS_CHART_VERSION" \
  --namespace argo --create-namespace -f ../k8s/training/argo-workflows-values.yaml --wait

echo "==> Building the trainer (CPU) and MLflow images and loading them into kind..."
docker build -f ../apps/finetune/Dockerfile -t "$IMAGE" ..
docker build -f ../apps/mlflow/Dockerfile -t "$MLFLOW_IMAGE" ..
kind load docker-image "$IMAGE" "$MLFLOW_IMAGE" --name "$CLUSTER_NAME"

echo "==> Applying MLflow and the workflow template..."
# Artifacts on MLflow's own volume: there's no S3 here.
kubectl -n training create configmap mlflow-storage \
  --from-literal=artifacts-destination=/mlflow/artifacts --dry-run=client -o yaml | kubectl apply -f -
sed "s#ghcr.io/yack300/llm-platform-mlflow:[^[:space:]]*#$MLFLOW_IMAGE#" ../k8s/training/mlflow.yaml \
  | kubectl apply -f -
kubectl apply -f ../k8s/training/finetune-workflow.yaml
kubectl -n training rollout status deployment/mlflow --timeout=5m

echo "==> Submitting the workflow (tiny model, a few steps, CPU)..."
WORKFLOW=$(kubectl create -o name -f - <<EOF
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  generateName: finetune-smoke-
  namespace: training
spec:
  workflowTemplateRef:
    name: finetune
  arguments:
    parameters:
      - name: image
        value: $IMAGE
      - name: base-model
        value: hf-internal-testing/tiny-random-MistralForCausalLM
      - name: train-args
        value: --max-steps 6 --grad-accum 1
      - name: evaluate-args
        value: --limit 6 --max-new-tokens 16
      - name: pod-spec-patch
        value: "{}"
EOF
)
echo "Submitted $WORKFLOW; waiting for it to finish (a few minutes)..."
kubectl -n training wait "$WORKFLOW" --for=jsonpath='{.status.phase}'=Succeeded --timeout=20m \
  || { kubectl -n training get "$WORKFLOW" -o jsonpath='{.status.phase}: {.status.message}{"\n"}'; exit 1; }
kubectl -n training get "$WORKFLOW" -o jsonpath='{.status.phase} in {.status.startedAt} -> {.status.finishedAt}{"\n"}'

echo "----------------------------------------------------------------------"
echo "MLflow:          kubectl -n training port-forward svc/mlflow 5000:5000"
echo "                 then http://localhost:5000 (experiment rag-behavior-lora)"
echo "Argo Workflows:  kubectl -n argo port-forward svc/argo-workflows-server 2746:2746"
echo "                 then http://localhost:2746"
echo "Delete when done: kind delete cluster --name $CLUSTER_NAME"
echo "----------------------------------------------------------------------"
