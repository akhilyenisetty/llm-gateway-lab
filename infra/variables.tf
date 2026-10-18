variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project_name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "llm-gateway"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.project_name))
    error_message = "Lowercase letters, digits, and dashes; at most 21 characters (load balancer names are short)."
  }
}

variable "certificate_arn" {
  description = <<-EOT
    ACM certificate for your domain, for HTTPS. Empty = plain HTTP on port 80, for a first
    try only: gateway keys and Claude Code logins would cross the internet unencrypted.
  EOT
  type        = string
  default     = ""
}

variable "admin_cidrs" {
  description = "Addresses allowed to reach the admin API (the full dashboard), e.g. [\"203.0.113.7/32\"]. Users' own pages are not limited."
  type        = list(string)
  default     = ["0.0.0.0/0"]
  validation {
    condition     = length(var.admin_cidrs) >= 1 && length(var.admin_cidrs) <= 4
    error_message = "Between 1 and 4 address ranges (a load balancer rule limit)."
  }
}

variable "image_tag" {
  description = "Image tag the services run; scripts/deploy.sh pushes :latest."
  type        = string
  default     = "latest"
}

variable "gateway_count" {
  description = <<-EOT
    Number of gateway containers. With 1, it migrates the database as it starts. With
    more, run scripts/migrate.sh before each deploy, and note that each container runs the
    background categorizer (a session may then be classified twice; a small extra cost).
  EOT
  type        = number
  default     = 1
  validation {
    condition     = var.gateway_count >= 1
    error_message = "At least one gateway."
  }
}

variable "gateway_cpu" {
  description = "Fargate CPU units for each gateway container (1024 = 1 vCPU)."
  type        = number
  default     = 512
}

variable "gateway_memory" {
  description = "Memory (MiB) for each gateway container; must be valid for gateway_cpu."
  type        = number
  default     = 1024
}

variable "request_timeout_s" {
  description = "Longest a call to Anthropic may take (UPSTREAM_TIMEOUT_S); the load balancer waits slightly longer."
  type        = number
  default     = 600
}

variable "gateway_settings" {
  description = "Non-secret gateway settings (see .env.example). Secrets are handled separately."
  type        = map(string)
  default = {
    STORE_CONTENT              = "false"
    DEFAULT_SESSION_BUDGET_USD = "5.00"
    ALLOW_CLIENT_BUDGETS       = "true"
    ALLOW_CLIENT_CREDENTIALS   = "true"
    ALERT_THRESHOLDS           = "[0.8, 1.0]"
  }
  validation {
    condition = length(setintersection(keys(var.gateway_settings), [
      "DATABASE_URL", "ANTHROPIC_API_KEY", "ADMIN_API_KEY", "RUN_MIGRATIONS", "UPSTREAM_TIMEOUT_S"
    ])) == 0
    error_message = "Secrets and RUN_MIGRATIONS/UPSTREAM_TIMEOUT_S are set by Terraform, not here."
  }
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_multi_az" {
  description = "A standby database in a second AZ that takes over on failure (about doubles the database cost)."
  type        = bool
  default     = false
}

variable "db_deletion_protection" {
  description = "Refuse to delete the database. Set false (and apply) before `terraform destroy`."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  type    = number
  default = 14
}
