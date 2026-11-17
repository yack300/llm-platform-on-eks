variable "aws_region" {
  description = "AWS region where the state bucket and lock table are created"
  type        = string
  default     = "us-east-1"
}

variable "state_bucket_name" {
  description = "S3 bucket name for Terraform state. Must be globally unique: suggested <your-name-or-account>-llm-platform-tfstate"
  type        = string
}

variable "artifacts_bucket_name" {
  description = "S3 bucket for MLflow artifacts and served LoRA adapters. Null (default) uses <state_bucket_name>-artifacts."
  type        = string
  default     = null
}
