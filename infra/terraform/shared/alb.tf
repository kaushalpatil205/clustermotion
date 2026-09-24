data "aws_elb_service_account" "this" {}

resource "aws_s3_bucket" "alb_logs" {
  bucket        = "${var.project}-alb-logs-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_lifecycle_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id
  rule {
    id     = "expire"
    status = "Enabled"
    filter {}
    expiration {
      days = 7
    }
  }
}

resource "aws_s3_bucket_policy" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = data.aws_elb_service_account.this.arn }
      Action    = "s3:PutObject"
      Resource  = "${aws_s3_bucket.alb_logs.arn}/alb/AWSLogs/${local.account_id}/*"
    }]
  })
}

resource "aws_security_group" "alb" {
  name        = "${var.project}-alb"
  description = "Public HTTP entrypoint of the shop"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description = "HTTP from the admin and the management node (load generator)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr, "${aws_eip.mgmt.public_ip}/32"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }
}

resource "aws_lb" "shop" {
  name               = var.project
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = module.vpc.public_subnets
  idle_timeout       = 30

  access_logs {
    bucket  = aws_s3_bucket.alb_logs.id
    prefix  = "alb"
    enabled = true
  }

  depends_on = [aws_s3_bucket_policy.alb_logs]
}

locals {
  colors = ["blue", "green"]
  service_colors = {
    for pair in setproduct(keys(var.services), local.colors) :
    "${pair[0]}-${pair[1]}" => { service = pair[0], color = pair[1] }
  }
}

resource "aws_lb_target_group" "svc" {
  for_each             = local.service_colors
  name                 = "cm-${each.key}"
  port                 = 8000
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = module.vpc.vpc_id
  deregistration_delay = 15

  health_check {
    path                = var.services[each.value.service].health_path
    interval            = 10
    healthy_threshold   = 2
    unhealthy_threshold = 2
    timeout             = 5
    matcher             = "200"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.shop.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "application/json"
      message_body = "{\"error\":\"no route\"}"
      status_code  = "404"
    }
  }
}

# Direct routes: X-CM-Target header pins a request to one colour.
resource "aws_lb_listener_rule" "direct" {
  for_each     = local.service_colors
  listener_arn = aws_lb_listener.http.arn
  # catalog: blue 10, green 15 · orders: blue 20, green 25 (below the weighted rules at 100+)
  priority = var.services[each.value.service].priority - 90 + (each.value.color == "blue" ? 0 : 5)

  condition {
    path_pattern {
      values = var.services[each.value.service].path_patterns
    }
  }
  condition {
    http_header {
      http_header_name = "X-CM-Target"
      values           = [each.value.color]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.svc[each.key].arn
  }
}

# Weighted routes: what real users hit. The engine owns the weights.
resource "aws_lb_listener_rule" "weighted" {
  for_each     = var.services
  listener_arn = aws_lb_listener.http.arn
  priority     = each.value.priority

  condition {
    path_pattern {
      values = each.value.path_patterns
    }
  }

  action {
    type = "forward"
    forward {
      target_group {
        arn    = aws_lb_target_group.svc["${each.key}-blue"].arn
        weight = 100
      }
      target_group {
        arn    = aws_lb_target_group.svc["${each.key}-green"].arn
        weight = 0
      }
    }
  }

  lifecycle {
    ignore_changes = [action]
  }
}
