output "cluster_name" {
  value = module.eks.cluster_name
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "configure_kubectl" {
  description = "Command to configure kubectl against this cluster"
  value       = "aws eks update-kubeconfig --name ${module.eks.cluster_name} --region ${var.aws_region}"
}


output "gpu_subnet_ids" {
  description = "Subnets the GPU node group can use (AZs that offer the GPU instance type)"
  value       = local.gpu_subnet_ids
}
