# RDS Module for the domain consumer
# Creates PostgreSQL RDS instance with pgvector support for Sense RAG
#
# Extracted from docs/_tools/docflow/deploy/terraform/modules/rds/
# as part of DocFlow → Sense consolidation (Step 0)

variable "name" {
  description = "Instance name"
  type        = string
  default     = "example-consumer"
}

variable "environment" {
  description = "Environment name (local, stage, prod)"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID"
  type        = string
}

variable "subnet_group_name" {
  description = "DB subnet group name"
  type        = string
}

variable "allowed_security_group_ids" {
  description = "Security group IDs allowed to access the database"
  type        = list(string)
  default     = []
}

variable "instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t3.medium"
}

variable "allocated_storage" {
  description = "Allocated storage in GB"
  type        = number
  default     = 20
}

variable "max_allocated_storage" {
  description = "Maximum allocated storage in GB for autoscaling"
  type        = number
  default     = 100
}

variable "engine_version" {
  description = "PostgreSQL version (must support pgvector)"
  type        = string
  default     = "15.4"
}

variable "database_name" {
  description = "Database name"
  type        = string
  default     = "neut_db"
}

variable "database_user" {
  description = "Database username"
  type        = string
  default     = "neut"
}

variable "multi_az" {
  description = "Enable Multi-AZ for production"
  type        = bool
  default     = false
}

variable "backup_retention_period" {
  description = "Backup retention period in days"
  type        = number
  default     = 7
}

variable "deletion_protection" {
  description = "Enable deletion protection"
  type        = bool
  default     = false
}

variable "skip_final_snapshot" {
  description = "Skip final snapshot on deletion"
  type        = bool
  default     = true
}

variable "performance_insights_enabled" {
  description = "Enable Performance Insights"
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}

# -----------------------------------------------------------------------------
# Resources
# -----------------------------------------------------------------------------

resource "random_password" "db_password" {
  length  = 32
  special = false
}

resource "aws_db_parameter_group" "pgvector" {
  name   = "${var.name}-${var.environment}-pgvector"
  family = "postgres15"

  parameter {
    name  = "shared_preload_libraries"
    value = "pg_stat_statements,pgvector"
  }

  tags = merge(var.tags, {
    Name        = "${var.name}-${var.environment}-pgvector"
    Environment = var.environment
  })
}

resource "aws_security_group" "rds" {
  name        = "${var.name}-${var.environment}-rds"
  description = "Security group for the domain consumer RDS"
  vpc_id      = var.vpc_id

  # Allow inbound from allowed security groups
  dynamic "ingress" {
    for_each = var.allowed_security_group_ids
    content {
      from_port       = 5432
      to_port         = 5432
      protocol        = "tcp"
      security_groups = [ingress.value]
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name        = "${var.name}-${var.environment}-rds"
    Environment = var.environment
  })
}

resource "aws_db_instance" "main" {
  identifier = "${var.name}-${var.environment}"

  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class

  allocated_storage     = var.allocated_storage
  max_allocated_storage = var.max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = var.database_name
  username = var.database_user
  password = random_password.db_password.result

  db_subnet_group_name   = var.subnet_group_name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.pgvector.name

  multi_az                     = var.multi_az
  backup_retention_period      = var.backup_retention_period
  backup_window                = "03:00-04:00"
  maintenance_window           = "sun:04:00-sun:05:00"
  deletion_protection          = var.deletion_protection
  skip_final_snapshot          = var.skip_final_snapshot
  final_snapshot_identifier    = var.skip_final_snapshot ? null : "${var.name}-${var.environment}-final"
  performance_insights_enabled = var.performance_insights_enabled

  tags = merge(var.tags, {
    Name        = "${var.name}-${var.environment}"
    Environment = var.environment
    ManagedBy   = "terraform"
  })

  lifecycle {
    prevent_destroy = false
  }
}

# -----------------------------------------------------------------------------
# Outputs
# -----------------------------------------------------------------------------

output "endpoint" {
  description = "RDS endpoint"
  value       = aws_db_instance.main.endpoint
}

output "address" {
  description = "RDS address"
  value       = aws_db_instance.main.address
}

output "port" {
  description = "RDS port"
  value       = aws_db_instance.main.port
}

output "database_name" {
  description = "Database name"
  value       = aws_db_instance.main.db_name
}

output "username" {
  description = "Database username"
  value       = aws_db_instance.main.username
}

output "password" {
  description = "Database password"
  value       = random_password.db_password.result
  sensitive   = true
}

output "security_group_id" {
  description = "Security group ID"
  value       = aws_security_group.rds.id
}

output "connection_string" {
  description = "PostgreSQL connection string"
  value       = "postgresql://${aws_db_instance.main.username}:${random_password.db_password.result}@${aws_db_instance.main.endpoint}/${aws_db_instance.main.db_name}"
  sensitive   = true
}
