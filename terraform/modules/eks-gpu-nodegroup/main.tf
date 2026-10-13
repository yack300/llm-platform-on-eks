# --------------------------------------------------------------------------
# Module: EKS GPU node group
#
# Nodes run Bottlerocket's NVIDIA variant, which ships the NVIDIA driver and
# runs the NVIDIA device plugin as a host service: nodes advertise
# nvidia.com/gpu with no DaemonSet in the cluster. Keep the device plugin on
# its default CDI strategy: Bottlerocket 1.66.0 doesn't mount the driver
# libraries into containers with the `envvar` strategy
# (bottlerocket-os/bottlerocket#4950).
#
# Bottlerocket also splits the OS volume (/dev/xvda) from the data volume
# (/dev/xvdb) that holds containerd's image store, so the data volume can be
# created from an EBS snapshot with the vLLM image already pulled (see
# scripts/build-gpu-image-cache.sh). That turns the image pull, the biggest
# part of a cold start, into reading a local disk.
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
  description = "Data volume size (GiB) for GPU nodes (containerd images and the Hugging Face cache). The vLLM image alone unpacks to well over 20 GiB. Must be at least the size of the image cache snapshot."
  type        = number
  default     = 100
}

variable "data_volume_snapshot_id" {
  description = "EBS snapshot to create the data volume from, with the vLLM image already pulled (scripts/build-gpu-image-cache.sh). null = empty volume, images are pulled at startup."
  type        = string
  default     = null
}

variable "data_volume_initialization_rate" {
  description = "MiB/s (100-300) at which EBS copies the snapshot onto a new data volume, only used with data_volume_snapshot_id. Billed per GiB of snapshot data on every volume created."
  type        = number
  default     = 300
}

resource "aws_launch_template" "gpu" {
  name_prefix            = "${var.cluster_name}-gpu-"
  update_default_version = true

  # Only the data volume is overridden; the OS volume (/dev/xvda) keeps the
  # AMI's default. A volume restored from a snapshot otherwise loads each
  # block from S3 on first read: on the second EKS run that made vLLM's
  # startup ~3x slower and the whole cold start slower than pulling the
  # image (~14.5 vs ~10 min). A provisioned initialization rate copies the
  # whole snapshot (~30 GiB) in ~100 s instead, for ~$0.0036/GiB per volume.
  block_device_mappings {
    device_name = "/dev/xvdb"

    ebs {
      volume_size           = var.disk_size
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
      snapshot_id           = var.data_volume_snapshot_id
      # AWS rejects a rate on a volume that isn't created from a snapshot.
      volume_initialization_rate = var.data_volume_snapshot_id != null ? var.data_volume_initialization_rate : null
    }
  }

  # IMDSv2 only, and unreachable from pods (hop limit 1): nothing on these
  # nodes needs it, workloads get AWS access through EKS Pod Identity.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "${var.cluster_name}-gpu-inference"
    }
  }
}

resource "aws_eks_node_group" "gpu" {
  cluster_name    = var.cluster_name
  node_group_name = "gpu-inference"
  node_role_arn   = aws_iam_role.gpu_node.arn
  subnet_ids      = var.subnet_ids

  # EKS picks the latest Bottlerocket NVIDIA AMI for the cluster version
  # (the launch template sets no image_id).
  ami_type = "BOTTLEROCKET_x86_64_NVIDIA"

  capacity_type  = var.use_spot ? "SPOT" : "ON_DEMAND"
  instance_types = [var.instance_type]

  # Disk size lives in the launch template. With the 20 GiB default the
  # vLLM image pull filled the disk: the kubelet evicted the pod on
  # DiskPressure and tainted the node (found on the first EKS run).
  launch_template {
    id      = aws_launch_template.gpu.id
    version = aws_launch_template.gpu.latest_version
  }

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
