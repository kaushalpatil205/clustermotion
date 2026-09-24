data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
}

# --- SQS: order events -------------------------------------------------------
resource "aws_sqs_queue" "orders_dlq" {
  name                      = "${var.project}-orders-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "orders" {
  name                       = "${var.project}-orders"
  visibility_timeout_seconds = 60
  receive_wait_time_seconds  = 5
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.orders_dlq.arn
    maxReceiveCount     = 10
  })
}

# --- DynamoDB: singleton lease + migration run timeline ----------------------
resource "aws_dynamodb_table" "leases" {
  name         = "${var.project}-leases"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "lease_id"

  attribute {
    name = "lease_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "runs" {
  name         = "${var.project}-runs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"
  range_key    = "ts"

  attribute {
    name = "run_id"
    type = "S"
  }

  attribute {
    name = "ts"
    type = "N"
  }
}

# --- S3: PostgreSQL WAL archive (both clusters), run artifacts ---------------
resource "aws_s3_bucket" "wal" {
  bucket        = "${var.project}-wal-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket" "artifacts" {
  bucket        = "${var.project}-artifacts-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "private" {
  for_each = {
    wal       = aws_s3_bucket.wal.id
    artifacts = aws_s3_bucket.artifacts.id
    alb_logs  = aws_s3_bucket.alb_logs.id
  }
  bucket                  = each.value
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# --- Database credentials (seeded into clusters by `cm register`) ------------
resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "db" {
  name                    = "${var.project}/orders-db"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id     = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({ username = "shop", password = random_password.db.result })
}

# --- Private DNS: the stable database name ----------------------------------
resource "aws_route53_zone" "internal" {
  name = "clustermotion.internal"
  vpc {
    vpc_id = module.vpc.vpc_id
  }
}

# --- ECR ---------------------------------------------------------------------
resource "aws_ecr_repository" "images" {
  for_each             = toset(["catalog", "orders", "fulfillment", "engine"])
  name                 = "${var.project}/${each.key}"
  image_tag_mutability = "MUTABLE" # "latest" is moved by CI; git-SHA tags are never reused
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "images" {
  for_each   = aws_ecr_repository.images
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 30 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 30 }
      action       = { type = "expire" }
    }]
  })
}
