#!/usr/bin/env bash
# What sharing a time-sliced T4 does to vLLM (docs/gpu-teams.md, phase C, on
# EKS): the benchmark with vLLM alone, and again with a research job computing
# in the GPU's other share.
#
# The job is a pod in the research-shared queue that multiplies matrices
# without pause, holding LOAD_GIB of GPU memory. It runs the vLLM image, which
# the node already has. Its log prints multiplications/s, to see the other
# side of the interference.
#
# Needs vLLM awake, LiteLLM at localhost:4000 (kubectl port-forward, see
# benchmark.py) and the cluster as the current kubectl context.
#
#   ./scripts/gpu-interference.sh [output-dir]
#   LOAD_GIB=12 ONLY_LOAD=1 ./scripts/gpu-interference.sh   # more than fits
#                                 next to vLLM: which of the two fails?
set -euo pipefail
cd "$(dirname "$0")"

OUT="${1:-.}"
LOAD_GIB="${LOAD_GIB:-4}"
CONCURRENCY="${CONCURRENCY:-1,4}"
REQUESTS="${REQUESTS:-6}"
POD=gpu-load
IMAGE=$(awk '/image: vllm\/vllm-openai/ {print $2}' ../k8s/vllm/deployment.yaml)

bench() { # <name>
  ./benchmark.py --concurrency "$CONCURRENCY" --requests-per-worker "$REQUESTS" --json "$OUT/interference-$1.json"
}
trap 'kubectl -n training delete pod $POD --ignore-not-found --wait=false >/dev/null' EXIT
kubectl -n training delete pod "$POD" --ignore-not-found --wait=true >/dev/null

if [ -z "${ONLY_LOAD:-}" ]; then
  echo "==> vLLM alone"
  bench alone
fi

echo "==> Starting the load ($LOAD_GIB GiB) in the other share"
kubectl apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $POD
  namespace: training
  labels:
    kueue.x-k8s.io/queue-name: research-shared
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  tolerations:
    - {key: nvidia.com/gpu, operator: Equal, value: present, effect: NoSchedule}
  containers:
    - name: load
      image: $IMAGE
      command: [python3, -u, -c]
      args:
        - |
          import time, torch
          hold = torch.empty(int($LOAD_GIB * 2**30), dtype=torch.uint8, device="cuda")
          a = torch.randn(4096, 4096, device="cuda")
          print("holding $LOAD_GIB GiB", flush=True)
          while True:
              n, start = 0, time.time()
              while time.time() - start < 10:
                  for _ in range(5):
                      b = a @ a
                  torch.cuda.synchronize()
                  n += 5
              print(f"{time.strftime('%H:%M:%S')} {n / (time.time() - start):.1f} matmul/s", flush=True)
      securityContext:
        allowPrivilegeEscalation: false
        privileged: false
      resources: # what vLLM leaves free on a g4dn.xlarge
        requests: {cpu: 500m, memory: 2Gi, nvidia.com/gpu: "1"}
        limits: {cpu: "1", memory: 4Gi, nvidia.com/gpu: "1"}
YAML
for _ in $(seq 1 60); do
  kubectl -n training logs "$POD" 2>/dev/null | grep -q "matmul/s" && break
  phase=$(kubectl -n training get pod "$POD" -o jsonpath='{.status.phase}')
  [ "$phase" = Failed ] && break
  sleep 5
done
kubectl -n training get pod "$POD" -o wide
kubectl -n training logs "$POD" | tail -5
kubectl -n llm-platform get pods -l app=vllm-inference -o wide
[ -n "${ONLY_LOAD:-}" ] && exit 0
# Sharing only means something on the same node
load_node=$(kubectl -n training get pod "$POD" -o jsonpath='{.spec.nodeName}')
vllm_node=$(kubectl -n llm-platform get pods -l app=vllm-inference -o jsonpath='{.items[0].spec.nodeName}')
[ "$load_node" = "$vllm_node" ] || { echo "Not on the same node ($load_node, $vllm_node)" >&2; exit 1; }
kubectl -n training logs "$POD" | grep -q "matmul/s" || { echo "The load isn't running" >&2; exit 1; }

echo "==> vLLM sharing the GPU"
bench shared
kubectl -n training logs "$POD" > "$OUT/interference-load.log"
echo "==> Load alone again (90 s, vLLM idle)"
sleep 90
kubectl -n training logs "$POD" > "$OUT/interference-load.log"
tail -15 "$OUT/interference-load.log"
