data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

resource "aws_key_pair" "mgmt" {
  key_name   = "${var.project}-mgmt"
  public_key = var.ssh_public_key
}

resource "aws_security_group" "mgmt" {
  name        = "${var.project}-mgmt"
  description = "Management node: k3s, Argo CD, Argo Workflows"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description = "SSH from admin"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }

  ingress {
    description = "k3s API from admin (kubectl)"
    from_port   = 6443
    to_port     = 6443
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_iam_role" "mgmt" {
  name = "${var.project}-mgmt"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "mgmt_ssm" {
  role       = aws_iam_role.mgmt.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "mgmt_ecr" {
  role       = aws_iam_role.mgmt.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# Everything the migration engine does, and nothing more.
resource "aws_iam_role_policy" "mgmt_engine" {
  name = "clustermotion-engine"
  role = aws_iam_role.mgmt.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "Eks", Effect = "Allow", Action = ["eks:DescribeCluster", "eks:ListClusters"], Resource = "*" },
      {
        Sid      = "AlbRead", Effect = "Allow",
        Action   = ["elasticloadbalancing:DescribeRules", "elasticloadbalancing:DescribeTargetHealth"],
        Resource = "*"
      },
      {
        Sid      = "AlbWeights", Effect = "Allow", Action = ["elasticloadbalancing:ModifyRule"],
        Resource = [for r in aws_lb_listener_rule.weighted : r.arn]
      },
      { Sid = "Metrics", Effect = "Allow", Action = ["cloudwatch:GetMetricData"], Resource = "*" },
      {
        Sid      = "Dns", Effect = "Allow",
        Action   = ["route53:ChangeResourceRecordSets", "route53:ListResourceRecordSets"],
        Resource = aws_route53_zone.internal.arn
      },
      {
        Sid      = "Dynamo", Effect = "Allow",
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"],
        Resource = [aws_dynamodb_table.leases.arn, aws_dynamodb_table.runs.arn]
      },
      {
        Sid      = "Logs", Effect = "Allow", Action = ["s3:ListBucket", "s3:GetObject"],
        Resource = [aws_s3_bucket.alb_logs.arn, "${aws_s3_bucket.alb_logs.arn}/*"]
      },
      { Sid = "Artifacts", Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.artifacts.arn}/*" },
      { Sid = "DbSecret", Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.db.arn },
    ]
  })
}

resource "aws_iam_instance_profile" "mgmt" {
  name = "${var.project}-mgmt"
  role = aws_iam_role.mgmt.name
}

resource "aws_instance" "mgmt" {
  ami                    = data.aws_ssm_parameter.ubuntu.value
  instance_type          = var.mgmt_instance_type
  subnet_id              = module.vpc.public_subnets[0]
  vpc_security_group_ids = [aws_security_group.mgmt.id]
  key_name               = aws_key_pair.mgmt.key_name
  iam_instance_profile   = aws_iam_instance_profile.mgmt.name

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2 # pods on k3s need IMDS for the instance role
  }

  root_block_device {
    volume_size = 40
    volume_type = "gp3"
    encrypted   = true
  }

  tags = { Name = "${var.project}-mgmt" }

  lifecycle {
    ignore_changes = [ami]
  }
}

resource "aws_eip" "mgmt" {
  domain   = "vpc"
  instance = aws_instance.mgmt.id
}
