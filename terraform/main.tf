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

locals {
  vpc_id     = coalesce(var.vpc_id, try(data.aws_vpc.default[0].id, null))
  subnet_ids = var.subnet_ids != null ? var.subnet_ids : data.aws_subnets.default[0].ids
}

# Karpenter's EC2NodeClasses find subnets and security groups by this tag.
# Default VPC subnets aren't managed here, so the tag is added on its own;
# Karpenter picks, per launch, a subnet in an AZ that offers the instance type.
resource "aws_ec2_tag" "karpenter_discovery" {
  for_each = toset(local.subnet_ids)

  resource_id = each.value
  key         = "karpenter.sh/discovery"
  value       = var.cluster_name
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
    # Lets pods assume IAM roles (EBS CSI driver, Karpenter).
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

  # Nodes launched by Karpenter share the platform nodes' security group
  # (selected by this tag), whose recommended rules already allow node-to-node
  # traffic: DNS, vLLM's port, metrics scraping.
  node_security_group_tags = {
    "karpenter.sh/discovery" = var.cluster_name
  }

  # Fixed-size base capacity: Argo CD, Karpenter itself (it can't run on the
  # nodes it manages), LiteLLM, Prometheus/Grafana, KEDA, Kyverno, Qdrant.
  # Anything that doesn't fit, and every GPU node, comes from Karpenter
  # (k8s/karpenter/). Spot with several 8 GiB types to improve the odds of
  # capacity; the managed node group replaces interrupted nodes.
  eks_managed_node_groups = {
    platform = {
      ami_type       = "AL2023_x86_64_STANDARD"
      capacity_type  = "SPOT"
      instance_types = ["t3.large", "t3a.large", "m5.large", "m5a.large"]
      min_size       = 2
      max_size       = 2
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

# --------------------------------------------------------------------------
# Karpenter: everything AWS-side it needs. The controller itself, its
# NodePools and EC2NodeClasses are deployed by Argo CD (k8s/argocd/apps/
# karpenter*.yaml, k8s/karpenter/), like every other in-cluster component.
#
# - Controller IAM role, bound through EKS Pod Identity to
#   kube-system/karpenter (the Helm chart's service account).
# - Node IAM role (+ EKS access entry) for the instances it launches. Names
#   are fixed so the EC2NodeClasses can reference them from Git.
# - SQS queue + EventBridge rules for spot interruptions and instance
#   health events, so Karpenter drains a node before AWS reclaims it.
#
# Instances Karpenter launches are not Terraform resources: teardown has to
# delete its NodePools first (scripts/teardown-eks.sh), or terraform destroy
# leaves them running.
# --------------------------------------------------------------------------
module "karpenter" {
  #checkov:skip=CKV_TF_1:Terraform Registry module pinned by version constraint and .terraform.lock.hcl, not a git source
  source  = "terraform-aws-modules/eks/aws//modules/karpenter"
  version = "~> 21.26"

  cluster_name = module.eks.cluster_name

  iam_role_name            = "${var.cluster_name}-karpenter"
  iam_role_use_name_prefix = false

  node_iam_role_name            = "${var.cluster_name}-karpenter-node"
  node_iam_role_use_name_prefix = false

  queue_name = "${var.cluster_name}-karpenter"
}
