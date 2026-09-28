# --------------------------------------------------------------------------
# Module: EKS GPU node group
#
# The NVIDIA device plugin (DaemonSet) is NOT installed here via Terraform:
# it's deployed as a manifest in k8s/gpu/nvidia-device-plugin.yaml,
# reconciled by FluxCD, following the same pattern as the rest of the repo
# (Terraform = AWS infra, Flux = cluster workloads). See that file for the
# toleration to the nvidia.com/gpu=present:NoSchedule taint defined below.
#
# Alternative if more complete management is needed in the future (drivers,
# container toolkit, DCGM exporter): NVIDIA GPU Operator, also via Flux.
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

variable "cluster_version" {
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

resource "aws_eks_node_group" "gpu" {
  cluster_name    = var.cluster_name
  node_group_name = "gpu-inference"
  node_role_arn   = aws_iam_role.gpu_node.arn
  subnet_ids      = var.subnet_ids

  ami_type = "AL2_x86_64_GPU" # EKS-optimized AMI with NVIDIA drivers preinstalled

  capacity_type  = var.use_spot ? "SPOT" : "ON_DEMAND"
  instance_types = [var.instance_type]

  scaling_config {
    desired_size = var.desired_size
    max_size     = 2
    min_size     = 0
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
