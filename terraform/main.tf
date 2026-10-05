terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.59"
    }
  }

  # Remote S3 backend with native S3 state locking (use_lockfile). Backend
  # blocks don't accept variables (they're resolved before the rest of the
  # config exists), so the actual config lives in backend.hcl (gitignored,
  # specific to each AWS account) and is passed via partial configuration:
  #
  #   1. cd bootstrap/ && terraform init && terraform apply
  #      (creates the S3 state bucket, run ONCE)
  #   2. Copy the `backend_hcl` output from that apply into terraform/backend.hcl
  #      (use backend.hcl.example as a template)
  #   3. cd .. && terraform init -backend-config=backend.hcl
  backend "s3" {}
}

provider "aws" {
  region = var.aws_region
}

# --------------------------------------------------------------------------
# Networking: the account's default VPC unless vpc_id/subnet_ids are given.
# Default VPC subnets are public, so nodes reach the internet (image pulls,
# Hugging Face) without a NAT Gateway, which would bill per hour even when
# the cluster is idle. Fine for a short-lived demo; a production cluster
# would put nodes in private subnets behind NAT.
# --------------------------------------------------------------------------
data "aws_availability_zones" "eks" {
  #checkov:skip=CKV_AWS_394:Only filters default-VPC subnets; a newly added AZ has no default subnet until one is created, so the set cannot silently expand
  state = "available"
  # AZs where the EKS control plane can't be placed (use1-az3 in us-east-1).
  exclude_zone_ids = var.excluded_az_ids
}

data "aws_vpc" "default" {
  count   = var.vpc_id == null ? 1 : 0
  default = true
}

data "aws_subnets" "default" {
  count = var.vpc_id == null ? 1 : 0

  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default[0].id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
  filter {
    name   = "availability-zone"
    values = data.aws_availability_zones.eks.names
  }
}

# Not every AZ offers every GPU instance type; the GPU node group only gets
# subnets where it does, so a scale-up never targets an AZ with no capacity.
data "aws_ec2_instance_type_offerings" "gpu" {
  location_type = "availability-zone"

  filter {
    name   = "instance-type"
    values = [var.gpu_instance_type]
  }
}

data "aws_subnet" "selected" {
  for_each = toset(local.subnet_ids)
  id       = each.value
}

locals {
  vpc_id     = coalesce(var.vpc_id, try(data.aws_vpc.default[0].id, null))
  subnet_ids = var.subnet_ids != null ? var.subnet_ids : data.aws_subnets.default[0].ids
  gpu_subnet_ids = [
    for id, s in data.aws_subnet.selected : id
    if contains(data.aws_ec2_instance_type_offerings.gpu.locations, s.availability_zone)
  ]
}

# --------------------------------------------------------------------------
# Minimal EKS cluster, reusing the official community module.
# A production setup would typically wrap this in an internal module; here
# the public module is used so the project is reproducible by anyone.
# --------------------------------------------------------------------------
module "eks" {
  #checkov:skip=CKV_TF_1:Terraform Registry module pinned by version constraint and .terraform.lock.hcl, not a git source
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.26"

  name               = var.cluster_name
  kubernetes_version = var.kubernetes_version

  vpc_id     = local.vpc_id
  subnet_ids = local.subnet_ids

  # kubectl/Argo CD bootstrap run from a laptop, so the API endpoint is
  # public, but only reachable from admin_cidrs (e.g. your current IP/32).
  endpoint_public_access       = true
  endpoint_public_access_cidrs = var.admin_cidrs

  # Whoever runs `terraform apply` gets cluster-admin via an EKS access
  # entry; without it the cluster is created but kubectl is denied.
  enable_cluster_creator_admin_permissions = true

  # Controllers get IAM via EKS Pod Identity (below), so no IRSA OIDC provider.
  enable_irsa = false

  # Control plane logs for a short-lived demo: keep a week, not the default 90 days.
  cloudwatch_log_group_retention_in_days = 7

  addons = {
    vpc-cni = {
      before_compute = true
    }
    kube-proxy = {}
    coredns    = {}
    # Lets pods assume IAM roles (EBS CSI driver, Cluster Autoscaler).
    eks-pod-identity-agent = {
      before_compute = true
    }
    # Without it, PVCs (Qdrant, Grafana) stay Pending.
    aws-ebs-csi-driver = {
      pod_identity_association = [{
        role_arn        = module.ebs_csi_pod_identity.iam_role_arn
        service_account = "ebs-csi-controller-sa"
      }]
    }
  }

  # Platform services (Argo CD, LiteLLM, Qdrant, Prometheus/Grafana, KEDA,
  # Kyverno). Spot with several 8 GiB types to improve the odds of capacity;
  # interruptions only restart stateless pods or reattach EBS volumes.
  eks_managed_node_groups = {
    platform = {
      ami_type       = "AL2023_x86_64_STANDARD"
      capacity_type  = "SPOT"
      instance_types = ["t3.large", "t3a.large", "m5.large", "m5a.large"]
      min_size       = 1
      max_size       = 3
      desired_size   = 2
    }
  }
}

# --------------------------------------------------------------------------
# IAM for in-cluster controllers via EKS Pod Identity (no OIDC provider or
# service account annotations needed).
# --------------------------------------------------------------------------
module "ebs_csi_pod_identity" {
  #checkov:skip=CKV_TF_1:Terraform Registry module pinned by version constraint and .terraform.lock.hcl, not a git source
  source  = "terraform-aws-modules/eks-pod-identity/aws"
  version = "~> 2.9"

  name                      = "${var.cluster_name}-ebs-csi"
  attach_aws_ebs_csi_policy = true
}

module "cluster_autoscaler_pod_identity" {
  #checkov:skip=CKV_TF_1:Terraform Registry module pinned by version constraint and .terraform.lock.hcl, not a git source
  source  = "terraform-aws-modules/eks-pod-identity/aws"
  version = "~> 2.9"

  name                             = "${var.cluster_name}-cluster-autoscaler"
  attach_cluster_autoscaler_policy = true
  cluster_autoscaler_cluster_names = [module.eks.cluster_name]

  # Must match the service account of the cluster-autoscaler Helm release
  # deployed by Argo CD (k8s/argocd/).
  associations = {
    cluster_autoscaler = {
      cluster_name    = module.eks.cluster_name
      namespace       = "kube-system"
      service_account = "cluster-autoscaler"
    }
  }
}

# --------------------------------------------------------------------------
# GPU node group, separate from the main module so it can be destroyed
# independently when not in use (cost control).
# --------------------------------------------------------------------------
module "gpu_nodegroup" {
  source = "./modules/eks-gpu-nodegroup"

  cluster_name  = module.eks.cluster_name
  subnet_ids    = local.gpu_subnet_ids
  instance_type = var.gpu_instance_type
  use_spot      = var.use_spot_gpu_nodes
  desired_size  = var.gpu_nodes_desired

  # Node groups need the cluster's networking/identity add-ons in place
  # before nodes can join.
  depends_on = [module.eks]
}
