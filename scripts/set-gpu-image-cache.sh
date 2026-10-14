#!/usr/bin/env bash
# Points the GPU nodes' data volume at the image cache snapshot (built by
# scripts/build-gpu-image-cache.sh), or back to an empty volume. It patches
# the `gpu` EC2NodeClass in the cluster; Argo CD ignores these two fields,
# since the snapshot is per account and can't live in Git. Karpenter treats
# the change as drift and replaces any running GPU node.
#
# With a snapshot, the volume also gets a provisioned initialization rate:
# without it, a restored volume loads each block lazily from S3, which made
# the cold start slower than pulling the image on the second EKS run.
#
# Usage:
#   ./scripts/set-gpu-image-cache.sh            # newest snapshot tagged by the build script
#   ./scripts/set-gpu-image-cache.sh snap-0123  # a specific snapshot
#   ./scripts/set-gpu-image-cache.sh off        # empty volume, pull the image
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
# MiB/s, 100-300. Billed per GiB of snapshot data, on every volume created
# (~30 GiB at ~$0.0036/GiB: ~$0.11 per cold start).
INIT_RATE="${INIT_RATE:-300}"

snapshot="${1:-}"
if [ -z "$snapshot" ]; then
  snapshot=$(aws ec2 describe-snapshots --region "$AWS_REGION" --owner-ids self \
    --filters "Name=tag:llm-platform/purpose,Values=gpu-image-cache" Name=status,Values=completed \
    --query 'sort_by(Snapshots,&StartTime)[-1].SnapshotId' --output text)
  if [ -z "$snapshot" ] || [ "$snapshot" = "None" ]; then
    echo "No image cache snapshot found; build one with scripts/build-gpu-image-cache.sh" >&2
    exit 1
  fi
fi

# The data volume is the second mapping in k8s/karpenter/gpu.yaml.
device=$(kubectl get ec2nodeclass gpu -o jsonpath='{.spec.blockDeviceMappings[1].deviceName}')
[ "$device" = "/dev/xvdb" ] || { echo "Expected /dev/xvdb as blockDeviceMappings[1], got '$device'" >&2; exit 1; }
path=/spec/blockDeviceMappings/1/ebs

if [ "$snapshot" = "off" ]; then
  for field in snapshotID volumeInitializationRate; do
    kubectl patch ec2nodeclass gpu --type=json -p "[{\"op\":\"remove\",\"path\":\"$path/$field\"}]" 2>/dev/null || true
  done
  echo "GPU data volume: empty (the vLLM image is pulled on each cold start)."
else
  kubectl patch ec2nodeclass gpu --type=json -p "[
    {\"op\":\"add\",\"path\":\"$path/snapshotID\",\"value\":\"$snapshot\"},
    {\"op\":\"add\",\"path\":\"$path/volumeInitializationRate\",\"value\":$INIT_RATE}
  ]" >/dev/null
  echo "GPU data volume: from $snapshot, initialized at $INIT_RATE MiB/s."
fi
kubectl get ec2nodeclass gpu -o jsonpath='{.spec.blockDeviceMappings[1].ebs}'; echo
