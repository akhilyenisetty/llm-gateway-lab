locals {
  scheme = var.certificate_arn != "" ? "https" : "http"
}

output "url" {
  description = "Load balancer address. With HTTPS, point your domain at it (a CNAME or Route 53 alias) and use the domain."
  value       = "${local.scheme}://${module.app.alb_dns_name}"
}

output "anthropic_key_secret" {
  description = "Put your Anthropic API key in this secret (once)."
  value       = aws_secretsmanager_secret.anthropic_api_key.name
}

output "admin_key_command" {
  description = "Prints the admin key (the dashboard sign-in)."
  value       = "aws secretsmanager get-secret-value --secret-id ${aws_secretsmanager_secret.admin_api_key.name} --query SecretString --output text"
}

output "gateway_repository_url" {
  value = module.app.gateway_repository_url
}

output "dashboard_repository_url" {
  value = module.app.dashboard_repository_url
}

output "cluster_name" {
  value = module.app.cluster_name
}

output "gateway_service_name" {
  value = module.app.gateway_service_name
}

output "dashboard_service_name" {
  value = module.app.dashboard_service_name
}

output "gateway_task_definition" {
  value = module.app.gateway_task_definition
}

output "private_subnet_ids" {
  value = module.network.private_subnet_ids
}

output "gateway_security_group_id" {
  value = module.app.gateway_security_group_id
}

output "gateway_log_group" {
  value = module.app.gateway_log_group
}

output "database_endpoint" {
  value = module.database.endpoint
}

output "region" {
  value = var.region
}
