terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Remote S3 + DynamoDB lock backend. Backend blocks don't accept variables
  # (they're resolved before the rest of the config exists), so the actual
  # config lives in backend.hcl (gitignored, specific to each AWS account)
  # and is passed via partial configuration:
  #
  #   1. cd bootstrap/ && terraform init && terraform apply
  #      (creates the S3 bucket + DynamoDB table, run ONCE)
  #   2. Copy the `backend_hcl` output from that apply into terraform/backend.hcl
  #      (use backend.hcl.example as a template)
  #   3. cd .. && terraform init -backend-config=backend.hcl
  backend "s3" {}
}

provider "aws" {
  region = var.aws_region
}

# --------------------------------------------------------------------------
# Minimal EKS cluster, reusing the official community module.
# In prod you'd normally use your own wrapper (like at Bolt/CPM); here the
# public module is used so the project is reproducible by anyone.
# --------------------------------------------------------------------------
module "eks" {
  #checkov:skip=CKV_TF_1:Terraform Registry module pinned by version constraint and .terraform.lock.hcl, not a git source
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = var.cluster_name
  cluster_version = var.kubernetes_version

  vpc_id     = var.vpc_id
  subnet_ids = var.subnet_ids

  # Base node group (no GPU) for platform services: LiteLLM, Qdrant, Prometheus/Grafana
  eks_managed_node_groups = {
    platform = {
      instance_types = ["t3.large"]
      min_size       = 1
      max_size       = 3
      desired_size   = 2
    }
  }
}

# --------------------------------------------------------------------------
# GPU node group, separate from the main module so it can be destroyed
# independently when not in use (cost control).
# --------------------------------------------------------------------------
module "gpu_nodegroup" {
  source = "./modules/eks-gpu-nodegroup"

  cluster_name    = module.eks.cluster_name
  cluster_version = var.kubernetes_version
  subnet_ids      = var.subnet_ids
  instance_type   = var.gpu_instance_type
  use_spot        = var.use_spot_gpu_nodes
  desired_size    = var.gpu_nodes_desired
}
