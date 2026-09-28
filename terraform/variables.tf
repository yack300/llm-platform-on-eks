variable "aws_region" {
  description = "AWS region where the cluster is created"
  type        = string
  default     = "us-east-1"
}

variable "cluster_name" {
  description = "EKS cluster name"
  type        = string
  default     = "llm-platform-demo"
}

variable "kubernetes_version" {
  description = "Kubernetes version for the cluster"
  type        = string
  default     = "1.30"
}

variable "vpc_id" {
  description = "VPC where the cluster is deployed (use an existing one or create one with the official vpc module)"
  type        = string
}

variable "subnet_ids" {
  description = "Private subnets for the node groups"
  type        = list(string)
}

variable "gpu_instance_type" {
  description = "GPU instance type for the inference node group. g4dn.xlarge is the cheapest GPU option on AWS."
  type        = string
  default     = "g4dn.xlarge"
}

variable "use_spot_gpu_nodes" {
  description = "Use spot instances for the GPU node group (significantly reduces cost, at the risk of interruption)"
  type        = bool
  default     = true
}

variable "gpu_nodes_desired" {
  description = "Desired number of GPU nodes. Keep at 0 when the project isn't actively in use to avoid incurring cost."
  type        = number
  default     = 0
}
