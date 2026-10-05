# --------------------------------------------------------------------------
# Bootstrap for Terraform's remote backend (S3, with native S3 state locking
# via use_lockfile: no DynamoDB table needed since Terraform 1.10).
#
# Applied ONCE, with local state (chicken-and-egg problem: you can't use the
# bucket as the backend of the same config that creates it). Flow:
#
#   cd terraform/bootstrap
#   terraform init
#   terraform apply
#
# Then copy the backend_hcl output into terraform/backend.hcl (see
# terraform/backend.hcl.example) and, in terraform/ (the main module), run:
#
#   cd terraform
#   terraform init -backend-config=backend.hcl
#
# Checkov skips below are deliberate: this bucket holds a few KB of state for
# a demo project, and each skipped control adds recurring cost (KMS keys, a
# second bucket, cross-region storage) for little risk reduction. Versioning,
# SSE, public access block and prevent_destroy cover the real risks.
# --------------------------------------------------------------------------

terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.59"
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
  #checkov:skip=CKV_AWS_18:Access logging needs a second bucket; not worth it for a single-user state bucket
  #checkov:skip=CKV_AWS_144:Cross-region replication doubles storage cost; state is reproducible via versioning
  #checkov:skip=CKV_AWS_145:SSE-S3 (AES256) instead of KMS to avoid per-key monthly cost
  #checkov:skip=CKV2_AWS_62:No consumer for S3 event notifications on the state bucket
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

# Versioning keeps every past state; expire old versions so storage doesn't
# grow forever, while keeping a 90-day recovery window.
resource "aws_s3_bucket_lifecycle_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    id     = "expire-noncurrent-state-versions"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.tfstate]
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
