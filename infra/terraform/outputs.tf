output "lambda_function_name" {
  description = "Monitor Lambda function name."
  value       = aws_lambda_function.monitor.function_name
}

output "lambda_log_group" {
  description = "CloudWatch log group for the monitor."
  value       = aws_cloudwatch_log_group.monitor.name
}

output "schedule_rule_name" {
  description = "EventBridge rule that triggers the monitor."
  value       = aws_cloudwatch_event_rule.schedule.name
}

output "sns_topic_arn" {
  description = "Alert topic ARN (CLOUDPULSE_SNS_TOPIC_ARN)."
  value       = aws_sns_topic.alerts.arn
}

output "health_table_name" {
  description = "CLOUDPULSE_HEALTH_TABLE"
  value       = aws_dynamodb_table.health_checks.name
}

output "incidents_table_name" {
  description = "CLOUDPULSE_INCIDENTS_TABLE"
  value       = aws_dynamodb_table.incidents.name
}

output "state_table_name" {
  description = "CLOUDPULSE_STATE_TABLE"
  value       = aws_dynamodb_table.monitor_state.name
}

output "services_table_name" {
  description = "CLOUDPULSE_SERVICES_TABLE"
  value       = aws_dynamodb_table.services.name
}

output "email_subscription_pending_confirmation" {
  description = "True when an email subscription was requested; it stays pending until the recipient confirms it."
  value       = var.alert_email != null
}

output "github_deploy_role_arn" {
  description = "Role for the manual GitHub Actions deploy workflow (AWS_DEPLOY_ROLE_ARN secret), if enabled."
  value       = one(aws_iam_role.github_deploy[*].arn)
}
