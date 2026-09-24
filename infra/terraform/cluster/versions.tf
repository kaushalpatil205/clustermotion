terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # One state per workspace: env/<workspace>/cluster/terraform.tfstate
  backend "s3" {
    key                  = "cluster/terraform.tfstate"
    workspace_key_prefix = "env"
    use_lockfile         = true
    encrypt              = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "clustermotion"
      ManagedBy = "terraform"
      Stack     = "cluster-${var.color}"
    }
  }
}
