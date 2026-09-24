locals {
  registry = "${local.account_id}.dkr.ecr.${var.region}.amazonaws.com"
}

output "vpc_id" { value = module.vpc.vpc_id }
output "vpc_cidr" { value = var.vpc_cidr }
output "private_subnets" { value = module.vpc.private_subnets }
output "mgmt_public_ip" { value = aws_eip.mgmt.public_ip }
output "mgmt_role_arn" { value = aws_iam_role.mgmt.arn }
output "mgmt_security_group_id" { value = aws_security_group.mgmt.id }
output "alb_security_group_id" { value = aws_security_group.alb.id }
output "alb_dns_name" { value = aws_lb.shop.dns_name }
output "image_registry" { value = local.registry }
output "queue_arn" { value = aws_sqs_queue.orders.arn }
output "lease_table_arn" { value = aws_dynamodb_table.leases.arn }
output "wal_bucket_arn" { value = aws_s3_bucket.wal.arn }
output "ci_role_arn" { value = try(aws_iam_role.ci[0].arn, "") }

output "engine_config" {
  description = "Write to build/config.json: terraform output -json engine_config > build/config.json"
  value = {
    project            = var.project
    region             = var.region
    clusters           = { blue = "cm-blue", green = "cm-green" }
    workload_namespace = "shop"
    alb = {
      dns_name          = aws_lb.shop.dns_name
      arn_suffix        = aws_lb.shop.arn_suffix
      logs_bucket       = aws_s3_bucket.alb_logs.id
      logs_prefix       = "alb"
      security_group_id = aws_security_group.alb.id
    }
    services = {
      for name, svc in var.services : name => {
        rule_arn        = aws_lb_listener_rule.weighted[name].arn
        health_path     = svc.health_path
        smoke_paths     = name == "catalog" ? ["/api/catalog/products", "/api/catalog/products/sku-100"] : ["/api/orders/readyz"]
        shadow_prefixes = ["/api/${name}/"]
        target_groups = {
          for color in local.colors : color => {
            arn        = aws_lb_target_group.svc["${name}-${color}"].arn
            arn_suffix = aws_lb_target_group.svc["${name}-${color}"].arn_suffix
          }
        }
      }
    }
    shift_order      = ["catalog", "orders"]
    lease            = { table = aws_dynamodb_table.leases.name, lease_id = "singletons", ttl_seconds = 30 }
    runs_table       = aws_dynamodb_table.runs.name
    artifacts_bucket = aws_s3_bucket.artifacts.id
    db = {
      zone_id        = aws_route53_zone.internal.zone_id
      record         = "db.clustermotion.internal"
      namespace      = "shop"
      cluster_prefix = "orders-db"
      lb_service     = "orders-db-lb"
      secret_id      = aws_secretsmanager_secret.db.name
      name           = "shop"
    }
    gitops = {
      argocd_namespace = "argocd"
      cluster_annotations = {
        "clustermotion.io/aws-region"     = var.region
        "clustermotion.io/vpc-id"         = module.vpc.vpc_id
        "clustermotion.io/vpc-cidr"       = var.vpc_cidr
        "clustermotion.io/image-registry" = local.registry
        "clustermotion.io/queue-url"      = aws_sqs_queue.orders.url
        "clustermotion.io/wal-bucket"     = aws_s3_bucket.wal.id
        "clustermotion.io/lease-table"    = aws_dynamodb_table.leases.name
        "clustermotion.io/db-host"        = "db.clustermotion.internal"
        "clustermotion.io/alb-sg"         = aws_security_group.alb.id
      }
    }
    slo = { max_5xx_ratio = 0.01, max_p95_ratio = 1.5, min_p95_seconds = 0.3, min_requests = 20 }
  }
}
