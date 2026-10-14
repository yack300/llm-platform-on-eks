#!/usr/bin/env bash
# Run BEFORE `terraform destroy`. Deletes everything Argo CD deployed so that
# the resources Kubernetes created outside Terraform are cleaned up too.
# The ones that matter for cost are Karpenter's instances and the EBS volumes
# behind PVCs (Qdrant, Grafana): destroying the cluster with them still in it
# orphans them, and they keep billing until deleted by hand.
#
# Usage:
#   export AWS_PROFILE=<your-profile>
#   ./scripts/teardown-eks.sh && (cd terraform && terraform destroy -var='admin_cidrs=["<ip>/32"]')
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-llm-platform-demo}"
AWS_REGION="${AWS_REGION:-us-east-1}"

aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$AWS_REGION" >/dev/null

# Karpenter's nodes aren't Terraform resources: they have to go while
# Karpenter is still running, or they're orphaned (and keep billing). The
# root app would recreate a deleted child app, so stop it from syncing,
# delete the NodePools' app (Karpenter then terminates its nodes) and wait.
echo "==> Removing Karpenter's nodes first..."
kubectl -n argocd patch application root --type=json \
  -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]' 2>/dev/null || true
kubectl -n argocd delete application karpenter-nodepools --ignore-not-found --timeout=10m
for _ in $(seq 1 60); do
  [ -z "$(kubectl get nodeclaims -o name 2>/dev/null)" ] && break
  sleep 10
done
kubectl get nodeclaims 2>/dev/null || true

echo "==> Deleting the root Application (cascades to every child Application)..."
# Each Application carries the resources finalizer, so deleting it deletes its
# resources first, PVCs included; the gp3 StorageClass (reclaimPolicy Delete)
# then deletes the EBS volumes.
kubectl -n argocd delete application root --ignore-not-found --timeout=15m

echo "==> Deleting PVCs left behind by StatefulSets..."
# Deleting a StatefulSet never deletes its volumeClaimTemplates PVCs (by
# design, to protect data), and Argo CD doesn't own them either: the
# StatefulSet controller created them. Qdrant's PVC survived the app deletion
# on the first EKS run.
kubectl delete pvc --all -n llm-platform --ignore-not-found --timeout=5m
kubectl delete pvc --all -n monitoring --ignore-not-found --timeout=5m

echo "==> Waiting for PersistentVolumes to be released..."
for _ in $(seq 1 60); do
  [ -z "$(kubectl get pv -o name 2>/dev/null)" ] && break
  sleep 10
done
kubectl get pv 2>/dev/null || true

echo "==> Checking for EBS volumes created by the EBS CSI driver..."
# The driver tags every volume it provisions with ebs.csi.aws.com/cluster=true
# (account-wide, not per cluster: this assumes it's the only EKS cluster in the
# region). Node root volumes don't carry it; terraform destroy removes those.
volumes=$(aws ec2 describe-volumes --region "$AWS_REGION" \
  --filters "Name=tag:ebs.csi.aws.com/cluster,Values=true" \
  --query 'Volumes[].[VolumeId,State,Size,Tags[?Key==`kubernetes.io/created-for/pvc/name`]|[0].Value]' --output text)
if [ -n "$volumes" ]; then
  echo "WARNING: EBS volumes created by the CSI driver still exist (they keep billing):"
  echo "$volumes"
  echo "Delete them once they're 'available': aws ec2 delete-volume --volume-id <id>"
  exit 1
fi

echo "==> Checking for instances launched by Karpenter..."
instances=$(aws ec2 describe-instances --region "$AWS_REGION" \
  --filters "Name=tag:karpenter.sh/nodepool,Values=*" Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[].Instances[].[InstanceId,InstanceType,Tags[?Key==`karpenter.sh/nodepool`]|[0].Value]' --output text)
if [ -n "$instances" ]; then
  echo "WARNING: instances launched by Karpenter are still running (they keep billing):"
  echo "$instances"
  echo "Terminate them: aws ec2 terminate-instances --instance-ids <id>"
  exit 1
fi

echo "No PVC-backed EBS volumes or Karpenter instances left. Safe to run terraform destroy."

# The GPU image cache snapshot (scripts/build-gpu-image-cache.sh) isn't
# managed by Terraform and survives the destroy on purpose, so the next run
# can reuse it; it bills ~$0.05/GB-month until deleted.
snapshots=$(aws ec2 describe-snapshots --region "$AWS_REGION" --owner-ids self \
  --filters "Name=tag:llm-platform/purpose,Values=gpu-image-cache" \
  --query 'Snapshots[].[SnapshotId,VolumeSize,StartTime]' --output text)
if [ -n "$snapshots" ]; then
  echo
  echo "Note: GPU image cache snapshot(s) kept for the next run (delete with aws ec2 delete-snapshot --snapshot-id <id>):"
  echo "$snapshots"
fi
