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

output "gpu_nodegroup_status" {
  description = "Remember to set gpu_nodes_desired = 0 when you're not using the project"
  value       = "Desired GPU nodes: ${var.gpu_nodes_desired}"
}
