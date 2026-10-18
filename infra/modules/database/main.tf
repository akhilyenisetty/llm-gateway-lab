# Postgres on RDS: AWS runs it, patches minor versions, and takes daily backups.
#
# It sits in the private subnets with no public address. Its security group (a firewall)
# starts with no inbound rules; the app module adds one that lets only the gateway's
# containers in, on 5432.
#
# The master password is generated here and stored, inside a full DATABASE_URL, in
# Secrets Manager, where ECS reads it at container start. It is also in Terraform state
# (any Terraform-made password is), which is why backend.tf recommends encrypted S3.

resource "random_password" "db" {
  length  = 32
  special = false # letters and digits only, so the URL needs no escaping
}

resource "aws_db_subnet_group" "this" {
  name       = var.name_prefix
  subnet_ids = var.subnet_ids
}

resource "aws_security_group" "db" {
  name        = "${var.name_prefix}-db"
  description = "Postgres; inbound rules are added for the gateway"
  vpc_id      = var.vpc_id
}

resource "aws_db_instance" "this" {
  identifier     = var.name_prefix
  engine         = "postgres"
  engine_version = "17" # major version; RDS applies minor upgrades in the maintenance window
  instance_class = var.instance_class

  db_name  = "gateway"
  username = "gateway"
  password = random_password.db.result

  allocated_storage     = 20
  max_allocated_storage = 100 # grows automatically when needed
  storage_type          = "gp3"
  storage_encrypted     = true

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = var.multi_az

  auto_minor_version_upgrade = true
  backup_retention_period    = var.backup_retention_days
  copy_tags_to_snapshot      = true

  # Protect the data: `terraform destroy` fails until deletion_protection is turned off,
  # and then leaves a final snapshot behind.
  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = false
  final_snapshot_identifier = "${var.name_prefix}-final"
}

# What the gateway reads as DATABASE_URL. RDS for Postgres 15+ only accepts encrypted
# connections; ssl=require tells asyncpg to use TLS.
resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${var.name_prefix}/database-url"
  recovery_window_in_days = 0 # delete immediately on destroy, so a re-create can reuse the name
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id
  secret_string = format(
    "postgresql+asyncpg://%s:%s@%s/%s?ssl=require",
    aws_db_instance.this.username,
    random_password.db.result,
    aws_db_instance.this.endpoint, # host:port
    aws_db_instance.this.db_name,
  )
}
