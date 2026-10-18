variable "name_prefix" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "database_security_group_id" {
  type = string
}

variable "gateway_secret_arns" {
  description = "Environment variable name => Secrets Manager ARN, read into the gateway at start."
  type        = map(string)
}

variable "gateway_settings" {
  description = "Plain (non-secret) gateway settings, as in .env.example."
  type        = map(string)
}

variable "image_tag" {
  type = string
}

variable "certificate_arn" {
  type = string
}

variable "admin_cidrs" {
  type = list(string)
}

variable "gateway_count" {
  type = number
}

variable "gateway_cpu" {
  type = number
}

variable "gateway_memory" {
  type = number
}

variable "request_timeout_s" {
  type = number
}

variable "log_retention_days" {
  type = number
}
