# Alarms notify the same SNS topic as incidents. Each alarm costs about
# USD 0.10/month; set enable_alarms = false to skip them. Custom metrics come
# from CloudWatch Embedded Metric Format log lines (namespace "CloudPulse",
# dimension Environment) — no PutMetricData calls or permissions.

locals {
  alarm_count   = var.enable_alarms ? 1 : 0
  emf_namespace = "CloudPulse"
  alarm_actions = [aws_sns_topic.alerts.arn]
}

# The function failed outright (bad config, every service failed, crash).
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  count = local.alarm_count

  alarm_name          = "${local.name_prefix}-monitor-errors"
  alarm_description   = "CloudPulse monitor Lambda invocations are failing."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.monitor.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# Runs are getting close to the timeout, so services may start being skipped.
resource "aws_cloudwatch_metric_alarm" "lambda_duration" {
  count = local.alarm_count

  alarm_name          = "${local.name_prefix}-monitor-duration"
  alarm_description   = "CloudPulse monitor runs use more than 80% of the Lambda timeout."
  namespace           = "AWS/Lambda"
  metric_name         = "Duration"
  dimensions          = { FunctionName = aws_lambda_function.monitor.function_name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = var.lambda_timeout_seconds * 1000 * 0.8
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

# Heartbeat: the schedule stopped invoking the monitor.
resource "aws_cloudwatch_metric_alarm" "heartbeat" {
  count = local.alarm_count

  alarm_name          = "${local.name_prefix}-monitor-heartbeat"
  alarm_description   = "CloudPulse monitor has not been invoked recently."
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = aws_lambda_function.monitor.function_name }
  statistic           = "Sum"
  period              = var.heartbeat_period_seconds
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
}

# Some services could not be processed (storage errors, corrupt state).
resource "aws_cloudwatch_metric_alarm" "service_errors" {
  count = local.alarm_count

  alarm_name          = "${local.name_prefix}-service-errors"
  alarm_description   = "CloudPulse could not process one or more services."
  namespace           = local.emf_namespace
  metric_name         = "ServiceErrors"
  dimensions          = { Environment = "production" }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

# Incident notifications could not be delivered.
resource "aws_cloudwatch_metric_alarm" "notification_failures" {
  count = local.alarm_count

  alarm_name          = "${local.name_prefix}-notification-failures"
  alarm_description   = "CloudPulse failed to publish incident notifications."
  namespace           = local.emf_namespace
  metric_name         = "NotificationFailures"
  dimensions          = { Environment = "production" }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}
