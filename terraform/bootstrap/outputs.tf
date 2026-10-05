output "state_bucket_name" {
  value = aws_s3_bucket.tfstate.id
}

output "backend_hcl" {
  description = "Content to save into terraform/backend.hcl and use with: terraform init -backend-config=backend.hcl"
  value       = <<-EOT
    bucket       = "${aws_s3_bucket.tfstate.id}"
    key          = "llm-platform-on-eks/terraform.tfstate"
    region       = "${var.aws_region}"
    use_lockfile = true
    encrypt      = true
  EOT
}
