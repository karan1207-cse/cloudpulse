variable "aws_region" {
  description = "AWS region to deploy into, e.g. ap-south-1."
  type        = string
}

variable "project_name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "cloudpulse"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.project_name))
    error_message = "project_name must be 2-21 lowercase letters, digits or hyphens."
  }
}

variable "environment" {
  description = "Deployment environment name (dev, staging, prod)."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be dev, staging or prod."
  }
}

variable "services_json" {
  description = "Optional JSON list of services fixed in configuration (same format as services.example.json). Services can also be added from the dashboard into the services table. Do not put secrets in URLs."
  type        = string
  default     = "[]"

  validation {
    condition     = can(tolist(jsondecode(var.services_json)))
    error_message = "services_json must be a JSON list (use [] for none)."
  }
}

variable "schedule_expression" {
  description = "EventBridge schedule for health checks, e.g. rate(5 minutes) or cron(0/10 * * * ? *)."
  type        = string
  default     = "rate(5 minutes)"

  validation {
    condition     = can(regex("^(rate|cron)\(.+\)$", var.schedule_expression))
    error_message = "schedule_expression must be a rate() or cron() expression."
  }
}

variable "min_check_interval_seconds" {
  description = "A service checked more recently than this is skipped (absorbs duplicate EventBridge deliveries). Keep below the schedule interval."
  type        = number
  default     = 120
}

variable "failure_threshold" {
  description = "Default consecutive failures before an incident opens."
  type        = number
  default     = 3

  validation {
    condition     = var.failure_threshold >= 1 && var.failure_threshold <= 100
    error_message = "failure_threshold must be between 1 and 100."
  }
}

variable "check_timeout_seconds" {
  description = "Default per-check HTTP timeout."
  type        = number
  default     = 5
}

variable "alert_email" {
  description = "Optional email address to subscribe to alerts. The recipient must confirm the subscription."
  type        = string
  default     = null
}

variable "lambda_runtime" {
  description = "Lambda Python runtime."
  type        = string
  default     = "python3.13"
}

variable "lambda_architecture" {
  description = "arm64 (cheaper) or x86_64."
  type        = string
  default     = "arm64"

  validation {
    condition     = contains(["arm64", "x86_64"], var.lambda_architecture)
    error_message = "lambda_architecture must be arm64 or x86_64."
  }
}

variable "lambda_timeout_seconds" {
  description = "Lambda timeout. Services that cannot finish in the remaining time are skipped and reported."
  type        = number
  default     = 60

  validation {
    condition     = var.lambda_timeout_seconds >= 10 && var.lambda_timeout_seconds <= 900
    error_message = "lambda_timeout_seconds must be between 10 and 900."
  }
}

variable "lambda_memory_mb" {
  description = "Lambda memory size."
  type        = number
  default     = 256
}

variable "reserved_concurrency" {
  description = "Reserved concurrency for the monitor (-1 = unreserved). Set to 1 to forbid overlapping runs if your account has spare concurrency."
  type        = number
  default     = -1
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention."
  type        = number
  default     = 14
}

variable "history_retention_days" {
  description = "Health-check history TTL in DynamoDB."
  type        = number
  default     = 30
}

variable "enable_point_in_time_recovery" {
  description = "Enable DynamoDB PITR on incidents and state (small extra cost)."
  type        = bool
  default     = false
}

variable "enable_deletion_protection" {
  description = "Protect DynamoDB tables from deletion (set true for prod)."
  type        = bool
  default     = false
}

variable "enable_alarms" {
  description = "Create CloudWatch alarms (about USD 0.10 per alarm per month)."
  type        = bool
  default     = true
}

variable "heartbeat_period_seconds" {
  description = "Alarm if the monitor is not invoked at all for two periods of this length. Must be longer than the schedule interval."
  type        = number
  default     = 900
}

variable "github_repository" {
  description = "Optional owner/name of the GitHub repository allowed to deploy via OIDC. null disables the deploy role."
  type        = string
  default     = null
}

variable "github_environment" {
  description = "GitHub Actions environment that may assume the deploy role."
  type        = string
  default     = "production"
}

variable "create_github_oidc_provider" {
  description = "Create the GitHub OIDC identity provider (false if it already exists in the account)."
  type        = bool
  default     = true
}

variable "terraform_state_bucket" {
  description = "Name of the S3 bucket holding remote state (grants the deploy role access). Required with github_repository."
  type        = string
  default     = null
}

variable "tags" {
  description = "Extra tags for every resource."
  type        = map(string)
  default     = {}
}
