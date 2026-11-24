#!/usr/bin/env bash
# Kueue's GPU queues on kind, no GPU: two "GPU nodes" that advertise a fake
# nvidia.com/gpu, as k8s/karpenter/gpu.yaml's: on-demand time-sliced in 2,
# spot whole. With the queues from k8s/teams/queues.yaml and Kueue with the
# EKS values, the scenario docs/gpu-teams.md describes:
#
#   1. research trains on its spot GPU and runs a small job on the share of
#      product's on-demand GPU that nobody uses; a second training waits
#   2. a minute later product asks for both on-demand shares: Kueue evicts
#      the small job, which Argo Workflows marks Error and retries as a new
#      pod that waits in the queue; training isn't touched
#   3. product scales back to 0: the waiting job is admitted again
#
# Prints how long each admission took. The pods only sleep: this checks
# quotas, borrowing and eviction, not GPU sharing (that needs a real GPU).
#
# A cluster of its own (an 8 GB laptop can't hold it next to the platform).
# Delete it when done:
#   kind delete cluster --name llm-teams-local
set -euo pipefail

cd "$(dirname "$0")"

CLUSTER_NAME="llm-teams-local"
KUEUE_CHART_VERSION="0.20.7"
ARGO_WORKFLOWS_CHART_VERSION="2.0.18"

ts() { date +%s; }
elapsed() { echo "$(( $(ts) - $1 ))s"; }
# Waits until a jsonpath query on a resource returns the expected value
wait_for() { # <timeout-seconds> <expected> <kubectl get args...>
  local timeout=$1 expected=$2; shift 2
  local start; start=$(ts)
  until [ "$(kubectl get "$@" 2>/dev/null)" = "$expected" ]; do
    if [ $(( $(ts) - start )) -ge "$timeout" ]; then
      echo "Timed out waiting for '$expected' from: kubectl get $*" >&2
      return 1
    fi
    sleep 1
  done
}
research_running() {
  kubectl get pods -n training -l kueue.x-k8s.io/queue-name \
    --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l | tr -d ' '
}
wait_research_running() { # <count> <timeout-seconds>
  local start; start=$(ts)
  until [ "$(research_running)" -ge "$1" ]; do
    [ $(( $(ts) - start )) -ge "$2" ] && return 1
    sleep 1
  done
}
show() {
  echo "--- Kueue workloads"
  kubectl get workloads -A -o custom-columns='NAMESPACE:.metadata.namespace,QUEUE:.spec.queueName,ADMITTED:.status.conditions[?(@.type=="Admitted")].status,FLAVOR:.status.admission.podSetAssignments[0].flavors.nvidia\.com/gpu,EVICTED:.status.conditions[?(@.type=="Evicted")].reason' --sort-by=.metadata.creationTimestamp
  echo "--- GPU pods"
  kubectl get pods -n training -l kueue.x-k8s.io/queue-name -o wide --no-headers 2>/dev/null | awk '{print "training     " $1, $3, $7}'
  kubectl get pods -n llm-platform -l app=vllm-standin -o wide --no-headers 2>/dev/null | awk '{print "llm-platform " $1, $3, $7}'
  echo "--- Cluster queues"
  kubectl get clusterqueues
}

echo "==> Creating kind cluster (control plane + 2 'GPU' workers)..."
if ! kind get clusters | grep -qx "$CLUSTER_NAME"; then
  kind create cluster --name "$CLUSTER_NAME" --config - <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
    labels:
      karpenter.sh/capacity-type: on-demand
  - role: worker
    labels:
      karpenter.sh/capacity-type: spot
EOF
else
  echo "Cluster $CLUSTER_NAME already exists, skipping creation."
fi
kubectl config use-context "kind-$CLUSTER_NAME"

echo "==> Making the workers look like time-sliced T4 nodes..."
for node in $(kubectl get nodes -l karpenter.sh/capacity-type -o jsonpath='{.items[*].metadata.name}'); do
  # The GPU NodePools' taint, and the GPU (or its 2 shares) as an extended
  # resource
  gpus=1
  [ "$(kubectl get node "$node" -o jsonpath='{.metadata.labels.karpenter\.sh/capacity-type}')" = on-demand ] && gpus=2
  kubectl taint node "$node" nvidia.com/gpu=present:NoSchedule --overwrite
  kubectl patch node "$node" --subresource=status --type=json \
    -p '[{"op": "add", "path": "/status/capacity/nvidia.com~1gpu", "value": "'"$gpus"'"}]'
done
# Everything else (Kueue, Argo Workflows) runs on the control plane, the
# platform nodes' role on EKS
kubectl taint node "$CLUSTER_NAME-control-plane" node-role.kubernetes.io/control-plane:NoSchedule- 2>/dev/null || true
kubectl get nodes -o custom-columns='NODE:.metadata.name,CAPACITY-TYPE:.metadata.labels.karpenter\.sh/capacity-type,GPU:.status.allocatable.nvidia\.com/gpu'

echo "==> Installing Kueue (EKS values) and Argo Workflows..."
# The teams' namespaces first: Argo Workflows' chart puts the workflow
# service account in training
for ns in llm-platform training; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
done
helm upgrade --install kueue oci://registry.k8s.io/kueue/charts/kueue --version "$KUEUE_CHART_VERSION" \
  --namespace kueue-system --create-namespace -f ../k8s/teams/kueue-values.yaml --wait
helm upgrade --install argo-workflows argo-workflows \
  --repo https://argoproj.github.io/argo-helm --version "$ARGO_WORKFLOWS_CHART_VERSION" \
  --namespace argo --create-namespace -f ../k8s/training/argo-workflows-values.yaml --wait

echo "==> Applying the teams' queues..."
kubectl apply -f ../k8s/teams/queues.yaml
wait_for 60 "True" clusterqueue product -o 'jsonpath={.status.conditions[?(@.type=="Active")].status}'
wait_for 60 "True" clusterqueue research -o 'jsonpath={.status.conditions[?(@.type=="Active")].status}'

# Clean slate when re-running
kubectl -n training delete workflows --all --wait=true >/dev/null
kubectl -n llm-platform delete deployment vllm-standin --ignore-not-found --wait=true >/dev/null

echo
echo "==> 1. research: two trainings (its GPU fits one) and a small job on a borrowed share"
start=$(ts)
kubectl create -f - >/dev/null <<'EOF'
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  name: research-sweep
  namespace: training
spec:
  entrypoint: sweep
  serviceAccountName: argo-workflow
  securityContext: # Argo's executor runs as non-root (argo-workflows-values.yaml)
    runAsNonRoot: true
    runAsUser: 10001
  templates:
    - name: sweep
      steps:
        - - name: run
            template: gpu-step
            arguments:
              parameters:
                - {name: queue, value: "{{item.queue}}"}
            withItems:
              - {name: train-a, queue: research}
              - {name: train-b, queue: research}
              - {name: small, queue: research-shared}
    # Queue label and retry as in k8s/training/finetune-workflow.yaml
    - name: gpu-step
      inputs:
        parameters:
          - name: queue
      metadata:
        labels:
          kueue.x-k8s.io/queue-name: "{{inputs.parameters.queue}}"
      retryStrategy:
        limit: "3"
        retryPolicy: OnError
      tolerations:
        - {key: nvidia.com/gpu, operator: Equal, value: present, effect: NoSchedule}
      container:
        image: busybox:1.37.0
        command: [sh, -c, "sleep 900"]
        resources:
          requests: {cpu: 10m, memory: 16Mi, nvidia.com/gpu: "1"}
          limits: {cpu: 100m, memory: 32Mi, nvidia.com/gpu: "1"}
EOF
wait_research_running 2 180 || true
running=$(research_running)
echo "$running of 3 research steps running (2 expected) after $(elapsed "$start")"
show

# Evict a step that has really been running. Argo recreates, under the same
# name, a pod deleted while it still has the step as Pending; once Running, a
# deleted pod is an error, and only a retry brings the step back.
sleep 60

echo
echo "==> 2. product: vLLM stand-in scales to 2 replicas (its whole on-demand GPU)"
start=$(ts)
kubectl apply -f - >/dev/null <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm-standin
  namespace: llm-platform
  labels:
    app: vllm-standin
    kueue.x-k8s.io/queue-name: product # as k8s/vllm/deployment.yaml
spec:
  replicas: 2
  selector:
    matchLabels:
      app: vllm-standin
  template:
    metadata:
      labels:
        app: vllm-standin
    spec:
      tolerations:
        - {key: nvidia.com/gpu, operator: Equal, value: present, effect: NoSchedule}
      containers:
        - name: main
          image: registry.k8s.io/pause:3.10
          resources:
            requests: {cpu: 10m, memory: 16Mi, nvidia.com/gpu: "1"}
            limits: {cpu: 100m, memory: 32Mi, nvidia.com/gpu: "1"}
EOF
wait_for 180 "2" deployment vllm-standin -n llm-platform -o 'jsonpath={.status.readyReplicas}'
echo "product's 2 replicas ready after $(elapsed "$start")"
sleep 20 # let Argo notice the evicted pod and retry the step
show
echo "--- research workflow steps"
kubectl -n training get workflow research-sweep \
  -o jsonpath='{range .status.nodes.*}{.displayName}{"\t"}{.phase}{"\t"}{.message}{"\n"}{end}' | grep -v '^research-sweep' | sort

echo
echo "==> 3. product scales to 0 (KEDA would, with no demand): the waiting step gets its share back"
start=$(ts)
kubectl -n llm-platform scale deployment vllm-standin --replicas=0 >/dev/null
wait_research_running 2 180 || true
running=$(research_running)
echo "$running of 3 research steps running (2 expected) again after $(elapsed "$start")"
show

echo "----------------------------------------------------------------------"
echo "Cleanup: kind delete cluster --name $CLUSTER_NAME"
