variable "name_prefix" {
  type = string
}

variable "cidr_block" {
  description = "Address range of the VPC."
  type        = string
  default     = "10.0.0.0/16"
}
