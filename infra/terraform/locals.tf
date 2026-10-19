locals {
  name_prefix = "${var.project_name}-${var.environment}"

  tags = merge(
    {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    },
    var.tags,
  )

  health_table_name    = "${local.name_prefix}-health-checks"
  incidents_table_name = "${local.name_prefix}-incidents"
  state_table_name     = "${local.name_prefix}-monitor-state"
  function_name        = "${local.name_prefix}-monitor"
  log_group_name       = "/aws/lambda/${local.name_prefix}-monitor"

  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
}
