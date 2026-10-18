output "alb_dns_name" {
  value = aws_lb.this.dns_name
}

output "gateway_repository_url" {
  value = aws_ecr_repository.gateway.repository_url
}

output "dashboard_repository_url" {
  value = aws_ecr_repository.dashboard.repository_url
}

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "gateway_service_name" {
  value = aws_ecs_service.gateway.name
}

output "dashboard_service_name" {
  value = aws_ecs_service.dashboard.name
}

output "gateway_task_definition" {
  value = aws_ecs_task_definition.gateway.family
}

output "gateway_security_group_id" {
  value = aws_security_group.gateway.id
}

output "gateway_log_group" {
  value = aws_cloudwatch_log_group.gateway.name
}
