variable "project" {
  description = "Name prefix for every resource"
  type        = string
  default     = "clustermotion"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "admin_cidr" {
  description = "Your public IP in CIDR form (x.x.x.x/32): SSH, k3s API and ALB access"
  type        = string
}

variable "ssh_public_key" {
  description = "Contents of your SSH public key for the management node"
  type        = string
}

variable "mgmt_instance_type" {
  type    = string
  default = "t3.medium"
}

variable "github_repository" {
  description = "owner/repo allowed to push images from GitHub Actions (empty = no CI role)"
  type        = string
  default     = ""
}

variable "services" {
  description = "Services exposed through the shared ALB"
  type = map(object({
    path_patterns = list(string)
    health_path   = string
    priority      = number
  }))
  default = {
    catalog = { path_patterns = ["/api/catalog/*"], health_path = "/api/catalog/healthz", priority = 100 }
    orders  = { path_patterns = ["/api/orders", "/api/orders/*"], health_path = "/api/orders/healthz", priority = 110 }
  }
}
