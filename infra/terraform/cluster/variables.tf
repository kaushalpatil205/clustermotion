variable "region" {
  type    = string
  default = "us-east-1"
}

variable "state_bucket" {
  description = "Terraform state bucket (to read the shared stack outputs)"
  type        = string
}

variable "color" {
  description = "blue or green"
  type        = string
  validation {
    condition     = contains(["blue", "green"], var.color)
    error_message = "color must be blue or green"
  }
}

variable "kubernetes_version" {
  description = "EKS Kubernetes version, e.g. 1.34 for blue and 1.36 for green"
  type        = string
}

variable "admin_cidr" {
  description = "Your public IP (x.x.x.x/32) for the public EKS endpoint"
  type        = string
}

variable "node_instance_types" {
  type    = list(string)
  default = ["t3.large", "t3a.large", "m5.large"]
}

variable "capacity_type" {
  description = "SPOT for cheap demos, ON_DEMAND for stable recordings"
  type        = string
  default     = "SPOT"
}
