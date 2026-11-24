#!/usr/bin/env bash
# OpenCost on the Kueue test cluster (run scripts/teams-local.sh first): a
# trimmed Prometheus, OpenCost with g4dn.xlarge prices on the fake GPU nodes,
# and a scenario of known length, so what OpenCost bills each team can be
# checked by hand (docs/gpu-teams.md).
#
#   1. research: the spot GPU + 1 borrowed share of the on-demand one
#   2. product scales to 2 replicas: it gets both on-demand shares back
#   3. product scales to 0: research borrows again
#
#   MINUTES=10     length of each phase
set -euo pipefail

cd "$(dirname "$0")"

CLUSTER_NAME="llm-teams-local"
PROMETHEUS_CHART_VERSION="91.9.0" # k8s/argocd/apps/kube-prometheus-stack.yaml
OPENCOST_CHART_VERSION="2.5.34"   # k8s/argocd/apps/opencost.yaml
MINUTES="${MINUTES:-10}"
PRICE_ON_DEMAND=0.526 # docs/cost-comparison.md
PRICE_SPOT=0.27

kubectl config use-context "kind-$CLUSTER_NAME" >/dev/null
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

echo "==> Clean slate..."
kubectl -n training delete workflows --all --wait=true >/dev/null
kubectl -n llm-platform delete deployment vllm-standin --ignore-not-found --wait=true >/dev/null
kubectl wait workloads --all -A --for=delete --timeout=2m 2>/dev/null || true

echo "==> Pricing the GPU nodes..."
for type in on-demand spot; do
  price=$PRICE_ON_DEMAND; [ "$type" = spot ] && price=$PRICE_SPOT
  kubectl annotate nodes -l "karpenter.sh/capacity-type=$type" --overwrite "opencost.io/node-gpu-hourly-cost=$price"
done
# As k8s/karpenter/gpu.yaml's gpu-shared NodePool
kubectl label nodes -l karpenter.sh/capacity-type=on-demand --overwrite nvidia.com/gpu.count=1 nvidia.com/gpu.replicas=2
kubectl label nodes -l karpenter.sh/capacity-type=spot nvidia.com/gpu.count- nvidia.com/gpu.replicas- >/dev/null

echo "==> Installing Prometheus (no Grafana) and OpenCost..."
helm upgrade --install prometheus kube-prometheus-stack \
  --repo https://prometheus-community.github.io/helm-charts --version "$PROMETHEUS_CHART_VERSION" \
  -n monitoring --create-namespace -f ../k8s/observability/prometheus-grafana-values.yaml \
  -f ../k8s/observability/prometheus-grafana-values-local.yaml --set grafana.enabled=false --wait --timeout 8m >/dev/null
helm upgrade --install opencost opencost \
  --repo https://opencost.github.io/opencost-helm-chart --version "$OPENCOST_CHART_VERSION" \
  -n opencost --create-namespace -f ../k8s/observability/opencost-values.yaml \
  -f ../k8s/observability/opencost-values-local.yaml --wait --timeout 5m >/dev/null
# Prices and labels are read at scrape time; let a few scrapes pass
sleep 120

START=$(now)
echo "==> Scenario starts $START ($MINUTES min per phase)"
kubectl create -f - >/dev/null <<'EOT'
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  name: research-sweep
  namespace: training
spec:
  entrypoint: sweep
  serviceAccountName: argo-workflow
  securityContext:
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
              - {name: train, queue: research}
              - {name: small, queue: research-shared}
    - name: gpu-step
      inputs:
        parameters:
          - name: queue
      metadata:
        labels:
          kueue.x-k8s.io/queue-name: "{{inputs.parameters.queue}}"
      retryStrategy: # an evicted step is an error to Argo (teams-local.sh)
        limit: "3"
        retryPolicy: OnError
      tolerations:
        - {key: nvidia.com/gpu, operator: Equal, value: present, effect: NoSchedule}
      container:
        image: busybox:1.37.0
        command: [sh, -c, "sleep 14400"]
        resources:
          requests: {cpu: 10m, memory: 16Mi, nvidia.com/gpu: "1"}
          limits: {cpu: 100m, memory: 32Mi, nvidia.com/gpu: "1"}
EOT
sleep $(( MINUTES * 60 ))

echo "==> $(now) product scales to 2"
kubectl apply -f - >/dev/null <<'EOT'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm-standin
  namespace: llm-platform
  labels:
    app: vllm-standin
    kueue.x-k8s.io/queue-name: product
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
EOT
sleep $(( MINUTES * 60 ))

echo "==> $(now) product scales to 0"
kubectl -n llm-platform scale deployment vllm-standin --replicas=0 >/dev/null
sleep $(( MINUTES * 60 ))
END=$(now)
echo "==> Scenario ends $END"
sleep 60 # last scrapes

kubectl -n opencost port-forward svc/opencost 9003:9003 >/dev/null 2>&1 &
kubectl -n monitoring port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 >/dev/null 2>&1 &
trap 'kill $(jobs -p) 2>/dev/null || true' EXIT
sleep 5
python3 - "$MINUTES" "$PRICE_ON_DEMAND" "$PRICE_SPOT" <<'EOT'
import sys
h, od, spot = float(sys.argv[1]) / 60, float(sys.argv[2]), float(sys.argv[3])
print("Expected by hand, on-demand GPU in 2 shares (phases at their nominal length):")
print(f"  product  used ${2 * h / 2 * od:.4f}  idle ${(1 + 1) * h / 2 * od:.4f} (one on-demand share in phases 1 and 3)")
print(f"  research used ${3 * h * spot + 2 * h / 2 * od:.4f} (the spot GPU all along + one on-demand share in phases 1 and 3)")
print(f"  both nodes, {3 * h:.2f} h: ${3 * h * (od + spot):.4f}")
EOT
echo
./gpu-chargeback.py --window "$START,$END" ${JSON:+--json "$JSON"}
