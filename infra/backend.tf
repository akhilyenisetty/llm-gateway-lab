# Where Terraform keeps its state (its record of what it created). By default that's a
# local file, terraform.tfstate, which is gitignored. The state holds the database
# password, so for anything long-lived keep it in an encrypted S3 bucket instead:
# uncomment this, fill in a bucket you own (for example the one from
# serverless-ai-api-terraform's bootstrap, with a different key), and run
# `terraform init -migrate-state`. use_lockfile stops two applies running at once
# (it replaces the older DynamoDB lock table).
#
# terraform {
#   backend "s3" {
#     bucket       = "<state bucket>"
#     key          = "llm-gateway-lab/terraform.tfstate"
#     region       = "us-east-1"
#     encrypt      = true
#     use_lockfile = true
#   }
# }
