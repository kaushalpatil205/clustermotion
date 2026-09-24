data "terraform_remote_state" "shared" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "shared/terraform.tfstate"
    region = var.region
  }
}

locals {
  shared = data.terraform_remote_state.shared.outputs
  name   = "cm-${var.color}"
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = local.name
  kubernetes_version = var.kubernetes_version

  vpc_id     = local.shared.vpc_id
  subnet_ids = local.shared.private_subnets

  endpoint_public_access       = true
  endpoint_public_access_cidrs = [var.admin_cidr]
  endpoint_private_access      = true

  # Never pay the 6x extended-support price silently.
  upgrade_policy = {
    support_type = "STANDARD"
  }

  enable_cluster_creator_admin_permissions = true

  access_entries = {
    mgmt = {
      principal_arn = local.shared.mgmt_role_arn
      policy_associations = {
        admin = {
          policy_arn   = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = { type = "cluster" }
        }
      }
    }
  }

  # The management node (Argo CD, engine) reaches the private endpoint.
  security_group_additional_rules = {
    mgmt_api = {
      description              = "Kubernetes API from the management node"
      type                     = "ingress"
      protocol                 = "tcp"
      from_port                = 443
      to_port                  = 443
      source_security_group_id = local.shared.mgmt_security_group_id
    }
  }

  addons = {
    vpc-cni                = { before_compute = true }
    eks-pod-identity-agent = { before_compute = true }
    kube-proxy             = {}
    coredns                = {}
    aws-ebs-csi-driver = {
      pod_identity_association = [{
        role_arn        = module.ebs_csi_pod_identity.iam_role_arn
        service_account = "ebs-csi-controller-sa"
      }]
    }
  }

  eks_managed_node_groups = {
    default = {
      instance_types = var.node_instance_types
      capacity_type  = var.capacity_type
      min_size       = 2
      max_size       = 4
      desired_size   = 2
      labels         = { "clustermotion.io/color" = var.color }
    }
  }
}
