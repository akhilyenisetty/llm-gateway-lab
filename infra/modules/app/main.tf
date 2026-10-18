# The running app: images, load balancer, and the two ECS services.
#
#   internet ──HTTPS──► ALB ──┬─ /v1/*, /gateway/*, /health, /admin/* ─► gateway service
#                             └─ everything else ──────────────────────► dashboard service
#
# Both services run on Fargate (AWS runs the servers; we only describe the containers) in
# the private subnets. Only the load balancer can reach them, and only the gateway can
# reach the database. The dashboard's nginx doesn't proxy here (GATEWAY_UPSTREAM is
# empty): the load balancer routes /admin and /gateway to the gateway itself.

locals {
  https          = var.certificate_arn != ""
  gateway_port   = 8000
  dashboard_port = 80
  # With one gateway, it runs migrations as it starts. With more, several would run them
  # at once, so migrations run separately (scripts/migrate.sh) before each deploy.
  run_migrations = var.gateway_count == 1
}

data "aws_region" "current" {}

# --- Images ------------------------------------------------------------------------------

resource "aws_ecr_repository" "gateway" {
  name                 = "${var.name_prefix}/gateway"
  image_tag_mutability = "MUTABLE" # deploys push :latest again (scripts/deploy.sh)
  force_delete         = true
  image_scanning_configuration {
    scan_on_push = true # AWS checks each image for known vulnerabilities
  }
}

resource "aws_ecr_repository" "dashboard" {
  name                 = "${var.name_prefix}/dashboard"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
  image_scanning_configuration {
    scan_on_push = true
  }
}

# Keep the last 10 images of each, so old ones don't pile up.
resource "aws_ecr_lifecycle_policy" "keep_recent" {
  for_each   = { gateway = aws_ecr_repository.gateway.name, dashboard = aws_ecr_repository.dashboard.name }
  repository = each.value
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the 10 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

# --- Logs --------------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "gateway" {
  name              = "/ecs/${var.name_prefix}/gateway"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "dashboard" {
  name              = "/ecs/${var.name_prefix}/dashboard"
  retention_in_days = var.log_retention_days
}

# --- Firewalls (security groups) ---------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-alb"
  description = "Load balancer: HTTP/HTTPS from the internet"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "alb" {
  for_each          = toset(local.https ? ["80", "443"] : ["80"])
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = tonumber(each.value)
  to_port           = tonumber(each.value)
  ip_protocol       = "tcp"
}

resource "aws_security_group" "gateway" {
  name        = "${var.name_prefix}-gateway"
  description = "Gateway containers: from the load balancer only"
  vpc_id      = var.vpc_id
}

resource "aws_security_group" "dashboard" {
  name        = "${var.name_prefix}-dashboard"
  description = "Dashboard containers: from the load balancer only"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "gateway_from_alb" {
  security_group_id            = aws_security_group.gateway.id
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = local.gateway_port
  to_port                      = local.gateway_port
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "dashboard_from_alb" {
  security_group_id            = aws_security_group.dashboard.id
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = local.dashboard_port
  to_port                      = local.dashboard_port
  ip_protocol                  = "tcp"
}

# The database accepts the gateway, and nothing else.
resource "aws_vpc_security_group_ingress_rule" "db_from_gateway" {
  security_group_id            = var.database_security_group_id
  referenced_security_group_id = aws_security_group.gateway.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

# Outbound: the load balancer reaches the containers; the containers reach the internet
# through the NAT (Anthropic, ECR, Secrets Manager, CloudWatch) and the database.
resource "aws_vpc_security_group_egress_rule" "all" {
  for_each = {
    alb       = aws_security_group.alb.id
    gateway   = aws_security_group.gateway.id
    dashboard = aws_security_group.dashboard.id
  }
  security_group_id = each.value
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# --- Load balancer -----------------------------------------------------------------------

resource "aws_lb" "this" {
  name               = var.name_prefix
  load_balancer_type = "application"
  subnets            = var.public_subnet_ids
  security_groups    = [aws_security_group.alb.id]
  # The ALB closes a connection after this many seconds with no bytes. A non-streaming
  # Claude call can run for minutes before its first byte, so this must outlast the
  # gateway's own upstream timeout (60s, the AWS default, would cut such calls off).
  idle_timeout = var.request_timeout_s + 30
}

resource "aws_lb_target_group" "gateway" {
  name                 = "${var.name_prefix}-gw"
  port                 = local.gateway_port
  protocol             = "HTTP"
  target_type          = "ip" # Fargate tasks are registered by IP
  vpc_id               = var.vpc_id
  deregistration_delay = 60 # on deploy, give in-flight calls (and streams) time to finish
  health_check {
    path    = "/health"
    matcher = "200"
  }
}

resource "aws_lb_target_group" "dashboard" {
  name                 = "${var.name_prefix}-dash"
  port                 = local.dashboard_port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 10
  health_check {
    path    = "/"
    matcher = "200"
  }
}

# With a certificate: HTTPS on 443, and port 80 only redirects to it. Without one: plain
# HTTP on 80, for a first try only; gateway keys would cross the internet unencrypted.
resource "aws_lb_listener" "https" {
  count             = local.https ? 1 : 0
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.dashboard.arn
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  dynamic "default_action" {
    for_each = local.https ? [1] : []
    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
  dynamic "default_action" {
    for_each = local.https ? [] : [1]
    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.dashboard.arn
    }
  }
}

locals {
  listener_arn = local.https ? aws_lb_listener.https[0].arn : aws_lb_listener.http.arn
}

# Routing rules, checked in priority order; anything unmatched goes to the dashboard.
# 1. The admin API, from allowed addresses only.
resource "aws_lb_listener_rule" "admin_allowed" {
  listener_arn = local.listener_arn
  priority     = 10
  condition {
    path_pattern {
      values = ["/admin/*"]
    }
  }
  condition {
    source_ip {
      values = var.admin_cidrs
    }
  }
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gateway.arn
  }
}

# 2. The admin API from anywhere else: refused at the load balancer.
resource "aws_lb_listener_rule" "admin_denied" {
  listener_arn = local.listener_arn
  priority     = 20
  condition {
    path_pattern {
      values = ["/admin/*"]
    }
  }
  action {
    type = "fixed-response"
    fixed_response {
      content_type = "application/json"
      status_code  = "403"
      message_body = jsonencode({ detail = "the admin API is not reachable from this address" })
    }
  }
}

# 3. The Anthropic API paths, the users' API, and the health check.
resource "aws_lb_listener_rule" "gateway" {
  listener_arn = local.listener_arn
  priority     = 30
  condition {
    path_pattern {
      values = ["/v1/*", "/gateway/*", "/health"]
    }
  }
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gateway.arn
  }
}

# --- Permissions -------------------------------------------------------------------------

# The execution role is what ECS itself uses to start a container: pull the image, write
# logs, and read the secrets into environment variables. The app makes no AWS calls of
# its own, so it gets no task role.
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = values(var.gateway_secret_arns)
  }
}

resource "aws_iam_role_policy" "read_secrets" {
  name   = "read-gateway-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

# --- Containers --------------------------------------------------------------------------

resource "aws_ecs_cluster" "this" {
  name = var.name_prefix
  setting {
    name  = "containerInsights"
    value = "enabled" # CPU, memory, and task metrics in CloudWatch
  }
}

resource "aws_ecs_task_definition" "gateway" {
  family                   = "${var.name_prefix}-gateway"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.gateway_cpu
  memory                   = var.gateway_memory
  execution_role_arn       = aws_iam_role.execution.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64" # Graviton: cheaper, and images build natively on Apple Silicon
  }

  container_definitions = jsonencode([{
    name         = "gateway"
    image        = "${aws_ecr_repository.gateway.repository_url}:${var.image_tag}"
    essential    = true
    portMappings = [{ containerPort = local.gateway_port, protocol = "tcp" }]
    # Plain settings. Anything in var.gateway_settings (STORE_CONTENT, budgets, models, ...)
    # is added; see .env.example for the full list.
    environment = [
      for k, v in merge(var.gateway_settings, {
        RUN_MIGRATIONS     = tostring(local.run_migrations)
        UPSTREAM_TIMEOUT_S = tostring(var.request_timeout_s)
      }) : { name = k, value = v }
    ]
    # Secrets: ECS reads them from Secrets Manager into environment variables at start.
    secrets = [for k, arn in var.gateway_secret_arns : { name = k, valueFrom = arn }]
    # On deploy, ECS sends SIGTERM and waits this long before killing the container;
    # uvicorn uses it to finish in-flight calls.
    stopTimeout = 120
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.gateway.name
        awslogs-region        = data.aws_region.current.region
        awslogs-stream-prefix = "gateway"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "dashboard" {
  family                   = "${var.name_prefix}-dashboard"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([{
    name         = "dashboard"
    image        = "${aws_ecr_repository.dashboard.repository_url}:${var.image_tag}"
    essential    = true
    portMappings = [{ containerPort = local.dashboard_port, protocol = "tcp" }]
    environment  = [{ name = "GATEWAY_UPSTREAM", value = "" }] # the ALB routes the API paths
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.dashboard.name
        awslogs-region        = data.aws_region.current.region
        awslogs-stream-prefix = "dashboard"
      }
    }
  }])
}

resource "aws_ecs_service" "gateway" {
  name            = "gateway"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.gateway.arn
  desired_count   = var.gateway_count
  launch_type     = "FARGATE"
  # Time to run migrations and start before the load balancer's health checks count.
  health_check_grace_period_seconds = 90

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.gateway.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.gateway.arn
    container_name   = "gateway"
    container_port   = local.gateway_port
  }
  # A deploy whose containers keep failing is stopped and rolled back automatically.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  depends_on = [aws_lb_listener.http, aws_lb_listener.https]
}

resource "aws_ecs_service" "dashboard" {
  name            = "dashboard"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.dashboard.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.dashboard.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.dashboard.arn
    container_name   = "dashboard"
    container_port   = local.dashboard_port
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  depends_on = [aws_lb_listener.http, aws_lb_listener.https]
}
