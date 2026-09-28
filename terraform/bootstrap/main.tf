# --------------------------------------------------------------------------
# Bootstrap for Terraform's remote backend (S3 + DynamoDB lock).
#
# Applied ONCE, with local state (chicken-and-egg problem: you can't use the
# bucket as the backend of the same config that creates it). Flow:
#
#   cd terraform/bootstrap
#   terraform init
#   terraform apply
#
# Then, in terraform/ (the main module), uncomment the backend "s3" block
# in main.tf and run:
#
#   cd terraform
#   terraform init -backend-config=../terraform/bootstrap/backend.hcl
#
# (backend.hcl is generated as an output of this bootstrap, see outputs.tf)
# --------------------------------------------------------------------------

terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  # No remote backend here on purpose: this config bootstraps the backend
  # that everything else will use. Its own state stays local (a single
  # file, applied once and almost never changed again).
}

provider "aws" {
  region = var.aws_region
}

resource "aws_s3_bucket" "tfstate" {
  bucket = var.state_bucket_name

  # Protection against accidental "terraform destroy" of the state bucket
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled" # allows recovering a previous state if something corrupts the current one
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "tfstate_lock" {
  name         = var.lock_table_name
  billing_mode = "PAY_PER_REQUEST" # no fixed cost while the project is idle
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }
}
