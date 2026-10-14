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


# Referenced by the Karpenter Helm values and EC2NodeClasses in k8s/ (names
# are fixed in main.tf so they can be written there ahead of time).
output "karpenter_node_role_name" {
  value = module.karpenter.node_iam_role_name
}

output "karpenter_interruption_queue" {
  value = module.karpenter.queue_name
}
