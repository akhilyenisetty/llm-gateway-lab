provider "aws" {
  region = var.region

  # Every resource gets these tags, so the whole stack is easy to find (and to cost) in AWS.
  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform"
    }
  }
}
