output "state_bucket_name" {
  value = aws_s3_bucket.tfstate.id
}

output "lock_table_name" {
  value = aws_dynamodb_table.tfstate_lock.name
}

output "backend_hcl" {
  description = "Content to save into terraform/backend.hcl and use with: terraform init -backend-config=backend.hcl"
  value       = <<-EOT
    bucket         = "${aws_s3_bucket.tfstate.id}"
    key            = "llm-platform-on-eks/terraform.tfstate"
    region         = "${var.aws_region}"
    dynamodb_table = "${aws_dynamodb_table.tfstate_lock.name}"
    encrypt        = true
  EOT
}
