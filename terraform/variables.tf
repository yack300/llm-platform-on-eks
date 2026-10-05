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
  description = "Kubernetes version for the cluster. Keep it in EKS standard support: versions in extended support bill a much higher hourly rate, and retired versions can't be created at all (check with `aws eks describe-cluster-versions`)."
  type        = string
  default     = "1.36"
}

variable "vpc_id" {
  description = "VPC where the cluster is deployed. Null (default) uses the account's default VPC, which avoids NAT Gateway cost."
  type        = string
  default     = null
}

variable "subnet_ids" {
  description = "Subnets for the cluster and node groups. Null (default) uses the default VPC's subnets. Must be set together with vpc_id."
  type        = list(string)
  default     = null

  validation {
    condition     = (var.subnet_ids == null) == (var.vpc_id == null)
    error_message = "Set both vpc_id and subnet_ids, or neither (to use the default VPC)."
  }
}

variable "excluded_az_ids" {
  description = "AZ IDs to skip because the EKS control plane can't use them (use1-az3 in us-east-1). Adjust when changing region."
  type        = list(string)
  default     = ["use1-az3"]
}

variable "admin_cidrs" {
  description = "CIDRs allowed to reach the public EKS API endpoint, e.g. [\"<your-ip>/32\"] (curl -s https://checkip.amazonaws.com). No default on purpose: never open the API to 0.0.0.0/0 by accident."
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
  description = "Initial number of GPU nodes. Keep at 0: after creation the Cluster Autoscaler adds a node when a vLLM pod is Pending and removes it when idle (Terraform ignores later changes)."
  type        = number
  default     = 0
}
