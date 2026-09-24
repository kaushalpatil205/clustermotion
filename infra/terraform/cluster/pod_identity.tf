module "ebs_csi_pod_identity" {
  source  = "terraform-aws-modules/eks-pod-identity/aws"
  version = "~> 2.0"

  name                      = "${local.name}-ebs-csi"
  attach_aws_ebs_csi_policy = true
}

module "lb_controller_pod_identity" {
  source  = "terraform-aws-modules/eks-pod-identity/aws"
  version = "~> 2.0"

  name                            = "${local.name}-aws-lbc"
  attach_aws_lb_controller_policy = true

  associations = {
    this = {
      cluster_name    = module.eks.cluster_name
      namespace       = "kube-system"
      service_account = "aws-load-balancer-controller"
    }
  }
}

locals {
  queue_arn = local.shared.queue_arn
  lease_arn = local.shared.lease_table_arn
  wal_arn   = local.shared.wal_bucket_arn

  # service account (namespace/name) => IAM statements
  workload_roles = {
    keda = {
      namespace       = "keda"
      service_account = "keda-operator"
      statements      = [{ actions = ["sqs:GetQueueAttributes"], resources = [local.queue_arn] }]
    }
    orders = {
      namespace       = "shop"
      service_account = "orders"
      statements      = [{ actions = ["sqs:SendMessage"], resources = [local.queue_arn] }]
    }
    sweeper = {
      namespace       = "shop"
      service_account = "order-sweeper"
      statements = [
        { actions = ["sqs:SendMessage"], resources = [local.queue_arn] },
        { actions = ["dynamodb:GetItem"], resources = [local.lease_arn] },
      ]
    }
    fulfillment = {
      namespace       = "shop"
      service_account = "fulfillment-worker"
      statements = [
        { actions = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"], resources = [local.queue_arn] },
        { actions = ["dynamodb:GetItem"], resources = [local.lease_arn] },
      ]
    }
    lease-agent = {
      namespace       = "shop"
      service_account = "lease-agent"
      statements      = [{ actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"], resources = [local.lease_arn] }]
    }
    # CloudNativePG names the Postgres pods' service account after the Cluster.
    orders-db = {
      namespace       = "shop"
      service_account = "orders-db-${var.color}"
      statements = [
        { actions = ["s3:ListBucket", "s3:GetBucketLocation"], resources = [local.wal_arn] },
        { actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], resources = ["${local.wal_arn}/*"] },
      ]
    }
  }
}

module "workload_pod_identity" {
  source   = "terraform-aws-modules/eks-pod-identity/aws"
  version  = "~> 2.0"
  for_each = local.workload_roles

  name                 = "${local.name}-${each.key}"
  attach_custom_policy = true
  policy_statements    = each.value.statements

  associations = {
    this = {
      cluster_name    = module.eks.cluster_name
      namespace       = each.value.namespace
      service_account = each.value.service_account
    }
  }
}
