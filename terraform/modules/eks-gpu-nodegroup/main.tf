# --------------------------------------------------------------------------
# Module: EKS GPU node group
#
# The NVIDIA device plugin (DaemonSet) is NOT installed here via Terraform:
# it's deployed as a manifest in k8s/gpu/nvidia-device-plugin.yaml,
# synced by Argo CD, following the same pattern as the rest of the repo
# (Terraform = AWS infra, Argo CD = cluster workloads). See that file for the
# toleration to the nvidia.com/gpu=present:NoSchedule taint defined below.
#
# Alternative if more complete management is needed in the future (drivers,
# container toolkit, DCGM exporter): NVIDIA GPU Operator, also via Argo CD.
# --------------------------------------------------------------------------

data "aws_iam_policy_document" "eks_node_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "gpu_node" {
  name               = "${var.cluster_name}-gpu-nodegroup"
  assume_role_policy = data.aws_iam_policy_document.eks_node_assume_role.json
}

# Minimum policies required by an EKS worker node (identical to what you'd
# use in the base non-GPU node group): networking (CNI), image registry
# access, and the kubelet's own operations against the control plane.
resource "aws_iam_role_policy_attachment" "gpu_node_worker" {
  role       = aws_iam_role.gpu_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "gpu_node_cni" {
  role       = aws_iam_role.gpu_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "gpu_node_ecr_readonly" {
  role       = aws_iam_role.gpu_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

variable "cluster_name" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "instance_type" {
  type = string
}

variable "use_spot" {
  type = bool
}

variable "desired_size" {
  type = number
}

variable "disk_size" {
  description = "Root volume size (GiB) for GPU nodes. The vLLM image alone unpacks to well over the 20 GiB default, plus the model weights and Hugging Face cache."
  type        = number
  default     = 100
}

resource "aws_eks_node_group" "gpu" {
  cluster_name    = var.cluster_name
  node_group_name = "gpu-inference"
  node_role_arn   = aws_iam_role.gpu_node.arn
  subnet_ids      = var.subnet_ids

  # EKS-optimized Amazon Linux 2023 AMI with NVIDIA drivers and container
  # toolkit preinstalled (Amazon Linux 2 AMIs aren't published for current
  # Kubernetes versions).
  ami_type = "AL2023_x86_64_NVIDIA"

  capacity_type  = var.use_spot ? "SPOT" : "ON_DEMAND"
  instance_types = [var.instance_type]

  # With the 20 GiB default the vLLM image pull filled the disk: the kubelet
  # evicted the pod on DiskPressure and tainted the node (found on the first
  # EKS run).
  disk_size = var.disk_size

  scaling_config {
    desired_size = var.desired_size
    max_size     = 2
    min_size     = 0
  }

  # After creation, the Cluster Autoscaler owns the node count (0 when idle,
  # up to max_size on demand); don't let the next apply reset it.
  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }

  taint {
    key    = "nvidia.com/gpu"
    value  = "present"
    effect = "NO_SCHEDULE"
  }

  depends_on = [
    aws_iam_role_policy_attachment.gpu_node_worker,
    aws_iam_role_policy_attachment.gpu_node_cni,
    aws_iam_role_policy_attachment.gpu_node_ecr_readonly,
  ]
}

# --------------------------------------------------------------------------
# Scale-from-zero hints for the Cluster Autoscaler. With desired_size = 0
# there's no node to inspect, so CA reads the ASG tags to know that a node
# from this group would offer nvidia.com/gpu and carry the GPU taint;
# without them, a Pending vLLM pod never triggers a scale-up. EKS already
# tags managed node group ASGs with k8s.io/cluster-autoscaler/enabled and
# k8s.io/cluster-autoscaler/<cluster>, so CA auto-discovers the group.
# --------------------------------------------------------------------------
locals {
  cluster_autoscaler_node_template_tags = {
    "k8s.io/cluster-autoscaler/node-template/resources/nvidia.com/gpu" = "1"
    "k8s.io/cluster-autoscaler/node-template/taint/nvidia.com/gpu"     = "present:NoSchedule"
  }
}

resource "aws_autoscaling_group_tag" "cluster_autoscaler_node_template" {
  for_each = local.cluster_autoscaler_node_template_tags

  autoscaling_group_name = aws_eks_node_group.gpu.resources[0].autoscaling_groups[0].name

  tag {
    key                 = each.key
    value               = each.value
    propagate_at_launch = false
  }
}
