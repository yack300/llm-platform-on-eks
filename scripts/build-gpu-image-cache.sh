#!/usr/bin/env bash
# Builds an EBS snapshot of a Bottlerocket data volume with the vLLM image
# already pulled, for the GPU nodes' data volume
# (terraform: -var gpu_image_cache_snapshot_id=<snapshot>). On a cold start
# the node then reads the image from its own disk instead of pulling ~8.7 GB
# from Docker Hub, which was over half of the first run's ~8-minute wake-up.
#
# How: launch a temporary Bottlerocket instance (same release as the GPU
# nodes, standard variant: pulling images needs no GPU), pull the image into
# containerd's k8s.io namespace through SSM, stop it, snapshot its data volume
# (/dev/xvdb), and clean up the instance and its IAM role.
#
# Cost: a few cents of m5.large time, plus ~$0.05/GB-month for the snapshot
# while it exists (only used blocks count, ~20 GB). The snapshot is NOT
# managed by Terraform: delete it when you no longer need it
#   aws ec2 delete-snapshot --snapshot-id <snapshot>
# Rebuild it whenever the vLLM image tag in k8s/vllm/deployment.yaml changes.
#
# Usage:
#   export AWS_PROFILE=<your-profile>
#   ./scripts/build-gpu-image-cache.sh
set -euo pipefail
cd "$(dirname "$0")/.."

AWS_REGION="${AWS_REGION:-us-east-1}"
CLUSTER_NAME="${CLUSTER_NAME:-llm-platform-demo}"
K8S_VERSION="${K8S_VERSION:-1.36}"
INSTANCE_TYPE="${INSTANCE_TYPE:-m5.large}"
# Same size as the GPU nodes' data volume (disk_size in Terraform): the
# volume created from the snapshot can't be smaller than the snapshot.
DATA_VOLUME_SIZE="${DATA_VOLUME_SIZE:-100}"

# The image to cache is read from the manifest so the two can't drift.
VLLM_IMAGE=$(awk '/image: vllm\/vllm-openai:/ {print $2; exit}' k8s/vllm/deployment.yaml)
[ -n "$VLLM_IMAGE" ] || { echo "vLLM image not found in k8s/vllm/deployment.yaml" >&2; exit 1; }
IMAGES="${IMAGES:-docker.io/$VLLM_IMAGE}"

ROLE_NAME="${CLUSTER_NAME}-image-cache-builder"
aws_() { aws --region "$AWS_REGION" "$@"; }

INSTANCE_ID=""
cleanup() {
  echo "==> Cleaning up..."
  if [ -n "$INSTANCE_ID" ]; then
    aws_ ec2 terminate-instances --instance-ids "$INSTANCE_ID" >/dev/null || true
    aws_ ec2 wait instance-terminated --instance-ids "$INSTANCE_ID" || true
  fi
  aws iam remove-role-from-instance-profile --instance-profile-name "$ROLE_NAME" --role-name "$ROLE_NAME" 2>/dev/null || true
  aws iam delete-instance-profile --instance-profile-name "$ROLE_NAME" 2>/dev/null || true
  for policy in AmazonSSMManagedInstanceCore AmazonEKSWorkerNodePolicy; do
    aws iam detach-role-policy --role-name "$ROLE_NAME" --policy-arn "arn:aws:iam::aws:policy/$policy" 2>/dev/null || true
  done
  aws iam delete-role --role-name "$ROLE_NAME" 2>/dev/null || true
}
trap cleanup EXIT

# Match the Bottlerocket release EKS currently uses for GPU nodes.
BR_VERSION=$(aws_ ssm get-parameter --name "/aws/service/bottlerocket/aws-k8s-${K8S_VERSION}-nvidia/x86_64/latest/image_version" --query Parameter.Value --output text)
AMI_ID=$(aws_ ssm get-parameter --name "/aws/service/bottlerocket/aws-k8s-${K8S_VERSION}/x86_64/${BR_VERSION}/image_id" --query Parameter.Value --output text)
echo "==> Bottlerocket ${BR_VERSION} (${AMI_ID}), caching: ${IMAGES}"

echo "==> Creating a temporary IAM role for SSM..."
aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document '{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]
}' >/dev/null
# SSM for the commands below; the EKS worker policy because Bottlerocket's
# Kubernetes variants call ec2:DescribeInstances at boot (pluto) and never
# finish booting, or start the SSM agent, without it (found on the first run).
for policy in AmazonSSMManagedInstanceCore AmazonEKSWorkerNodePolicy; do
  aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "arn:aws:iam::aws:policy/$policy"
done
aws iam create-instance-profile --instance-profile-name "$ROLE_NAME" >/dev/null
aws iam add-role-to-instance-profile --instance-profile-name "$ROLE_NAME" --role-name "$ROLE_NAME"
sleep 15 # a new instance profile isn't usable by EC2 right away

# Default VPC subnets give the instance a public IP, so it reaches SSM and
# Docker Hub without a NAT Gateway (same as the cluster nodes).
VPC_ID=$(aws_ ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
# Not every AZ offers every instance type (m5.large isn't in us-east-1e).
AZS=$(aws_ ec2 describe-instance-type-offerings --location-type availability-zone \
  --filters "Name=instance-type,Values=$INSTANCE_TYPE" --query 'InstanceTypeOfferings[].Location' --output text | tr '\t' ',')
SUBNET_ID=$(aws_ ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC_ID" Name=default-for-az,Values=true \
  "Name=availability-zone,Values=$AZS" --query 'Subnets[0].SubnetId' --output text)

# The admin container gives SSM commands a root shell on the host (sheltie).
# AWS CLI v2 base64-encodes --user-data itself, so it's passed as plain TOML.
USER_DATA=$(printf '[settings.host-containers.admin]\nenabled = true\n')

echo "==> Launching the builder instance..."
INSTANCE_ID=$(aws_ ec2 run-instances \
  --image-id "$AMI_ID" --instance-type "$INSTANCE_TYPE" --subnet-id "$SUBNET_ID" \
  --iam-instance-profile "Name=$ROLE_NAME" \
  --user-data "$USER_DATA" \
  --metadata-options HttpTokens=required,HttpEndpoint=enabled \
  --block-device-mappings "DeviceName=/dev/xvdb,Ebs={VolumeSize=${DATA_VOLUME_SIZE},VolumeType=gp3,Encrypted=true,DeleteOnTermination=true}" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${CLUSTER_NAME}-image-cache-builder}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "    $INSTANCE_ID"

echo "==> Waiting for the SSM agent (a few minutes)..."
for _ in $(seq 1 60); do
  status=$(aws_ ssm describe-instance-information --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
    --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || true)
  [ "$status" = "Online" ] && break
  sleep 10
done
[ "$status" = "Online" ] || { echo "SSM agent never came online" >&2; exit 1; }

# The admin container starts a bit after the SSM agent comes online.
# shellcheck disable=SC2016 # expanded on the instance, not here
commands='"set -e","for i in $(seq 1 30); do apiclient exec admin true && break; sleep 10; done"'
# The managed label makes the kubelet treat the images as its own (otherwise
# its image GC and pull logic ignore them).
for image in $IMAGES; do
  commands+=",\"apiclient exec admin sheltie ctr -a /run/containerd/containerd.sock -n k8s.io images pull --label io.cri-containerd.image=managed $image\""
done
commands+=',"apiclient exec admin sheltie ctr -a /run/containerd/containerd.sock -n k8s.io images ls -q"'

echo "==> Pulling the images on the instance (~5-10 min)..."
COMMAND_ID=$(aws_ ssm send-command --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript --timeout-seconds 600 \
  --parameters "{\"commands\":[${commands}],\"executionTimeout\":[\"3600\"]}" \
  --query Command.CommandId --output text)
while :; do
  state=$(aws_ ssm get-command-invocation --command-id "$COMMAND_ID" --instance-id "$INSTANCE_ID" \
    --query Status --output text 2>/dev/null || echo Pending)
  case "$state" in
    Success) break ;;
    Pending|InProgress|Delayed) sleep 20 ;;
    *)
      aws_ ssm get-command-invocation --command-id "$COMMAND_ID" --instance-id "$INSTANCE_ID" \
        --query '[StandardOutputContent,StandardErrorContent]' --output text >&2 || true
      echo "Image pull failed: $state" >&2; exit 1 ;;
  esac
done
aws_ ssm get-command-invocation --command-id "$COMMAND_ID" --instance-id "$INSTANCE_ID" \
  --query StandardOutputContent --output text | tail -5

echo "==> Stopping the instance and snapshotting its data volume..."
aws_ ec2 stop-instances --instance-ids "$INSTANCE_ID" >/dev/null
aws_ ec2 wait instance-stopped --instance-ids "$INSTANCE_ID"
VOLUME_ID=$(aws_ ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query "Reservations[0].Instances[0].BlockDeviceMappings[?DeviceName=='/dev/xvdb'].Ebs.VolumeId" --output text)
SNAPSHOT_ID=$(aws_ ec2 create-snapshot --volume-id "$VOLUME_ID" \
  --description "Bottlerocket ${BR_VERSION} data volume with: ${IMAGES}" \
  --tag-specifications "ResourceType=snapshot,Tags=[{Key=Name,Value=${CLUSTER_NAME}-gpu-image-cache},{Key=llm-platform/purpose,Value=gpu-image-cache}]" \
  --query SnapshotId --output text)
echo "    $SNAPSHOT_ID (waiting for it to complete, ~10-20 min)..."
# `aws ec2 wait snapshot-completed` gives up after 10 minutes; poll instead.
while :; do
  read -r state progress < <(aws_ ec2 describe-snapshots --snapshot-ids "$SNAPSHOT_ID" \
    --query 'Snapshots[0].[State,Progress]' --output text)
  [ "$state" = "completed" ] && break
  [ "$state" = "error" ] && { echo "Snapshot failed" >&2; exit 1; }
  echo "    $progress"
  sleep 30
done

echo
echo "Done. Use it with:"
echo "  terraform apply -var='admin_cidrs=[\"<ip>/32\"]' -var='gpu_image_cache_snapshot_id=${SNAPSHOT_ID}'"
