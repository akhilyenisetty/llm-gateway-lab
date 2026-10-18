# llm-gateway-lab on AWS: ECS Fargate + RDS Postgres behind an Application Load Balancer.
#
#   network  -> VPC, public subnets (load balancer, NAT), private subnets (containers, DB)
#   database -> RDS Postgres, and DATABASE_URL in Secrets Manager
#   secrets  -> the Anthropic API key (value set by you, outside Terraform) and the admin key
#   app      -> ECR images, load balancer + routing, the gateway and dashboard services
#
# Step-by-step use: docs/DEPLOY.md.

module "network" {
  source      = "./modules/network"
  name_prefix = var.project_name
}

module "database" {
  source              = "./modules/database"
  name_prefix         = var.project_name
  vpc_id              = module.network.vpc_id
  subnet_ids          = module.network.private_subnet_ids
  instance_class      = var.db_instance_class
  multi_az            = var.db_multi_az
  deletion_protection = var.db_deletion_protection
}

# The real Anthropic key. Terraform creates the empty secret; you put the value in once:
#   aws secretsmanager put-secret-value --secret-id <output anthropic_key_secret> --secret-string 'sk-ant-...'
# so the key never appears in Terraform files or state.
resource "aws_secretsmanager_secret" "anthropic_api_key" {
  name                    = "${var.project_name}/anthropic-api-key"
  recovery_window_in_days = 0
}

# The dashboard/admin API key, generated here (read it with the command in the outputs).
resource "random_password" "admin_api_key" {
  length  = 43
  special = false
}

resource "aws_secretsmanager_secret" "admin_api_key" {
  name                    = "${var.project_name}/admin-api-key"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "admin_api_key" {
  secret_id     = aws_secretsmanager_secret.admin_api_key.id
  secret_string = random_password.admin_api_key.result
}

module "app" {
  source             = "./modules/app"
  name_prefix        = var.project_name
  vpc_id             = module.network.vpc_id
  public_subnet_ids  = module.network.public_subnet_ids
  private_subnet_ids = module.network.private_subnet_ids

  database_security_group_id = module.database.security_group_id
  gateway_secret_arns = {
    DATABASE_URL      = module.database.database_url_secret_arn
    ANTHROPIC_API_KEY = aws_secretsmanager_secret.anthropic_api_key.arn
    ADMIN_API_KEY     = aws_secretsmanager_secret.admin_api_key.arn
  }
  gateway_settings = var.gateway_settings

  image_tag          = var.image_tag
  certificate_arn    = var.certificate_arn
  admin_cidrs        = var.admin_cidrs
  gateway_count      = var.gateway_count
  gateway_cpu        = var.gateway_cpu
  gateway_memory     = var.gateway_memory
  request_timeout_s  = var.request_timeout_s
  log_retention_days = var.log_retention_days
}
