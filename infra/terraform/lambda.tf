# Package the pure-Python source. boto3 is provided by the Lambda runtime,
# so there are no third-party dependencies to bundle.
data "archive_file" "monitor" {
  type        = "zip"
  source_dir  = "${path.module}/../../src"
  output_path = "${path.module}/build/cloudpulse-lambda.zip"
  excludes    = ["cloudpulse/__pycache__", "cloudpulse.egg-info"]
}

resource "aws_cloudwatch_log_group" "monitor" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "monitor" {
  function_name                  = local.function_name
  description                    = "CloudPulse scheduled health checks and incident detection"
  role                           = aws_iam_role.monitor.arn
  handler                        = "cloudpulse.aws_lambda.handler"
  runtime                        = var.lambda_runtime
  architectures                  = [var.lambda_architecture]
  filename                       = data.archive_file.monitor.output_path
  source_code_hash               = data.archive_file.monitor.output_base64sha256
  timeout                        = var.lambda_timeout_seconds
  memory_size                    = var.lambda_memory_mb
  reserved_concurrent_executions = var.reserved_concurrency

  environment {
    variables = {
      CLOUDPULSE_ENV                        = "production"
      CLOUDPULSE_BACKEND                    = "aws"
      CLOUDPULSE_SERVICES                   = var.services_json
      CLOUDPULSE_HEALTH_TABLE               = aws_dynamodb_table.health_checks.name
      CLOUDPULSE_INCIDENTS_TABLE            = aws_dynamodb_table.incidents.name
      CLOUDPULSE_STATE_TABLE                = aws_dynamodb_table.monitor_state.name
      CLOUDPULSE_SERVICES_TABLE             = aws_dynamodb_table.services.name
      CLOUDPULSE_SNS_TOPIC_ARN              = aws_sns_topic.alerts.arn
      CLOUDPULSE_FAILURE_THRESHOLD          = tostring(var.failure_threshold)
      CLOUDPULSE_TIMEOUT_SECONDS            = tostring(var.check_timeout_seconds)
      CLOUDPULSE_HISTORY_RETENTION_DAYS     = tostring(var.history_retention_days)
      CLOUDPULSE_MIN_CHECK_INTERVAL_SECONDS = tostring(var.min_check_interval_seconds)
      CLOUDPULSE_LOG_LEVEL                  = "INFO"
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.monitor,
    aws_iam_role_policy.monitor,
  ]
}

# Scheduled invocations are asynchronous. Failed runs are not retried: the
# next scheduled run checks again, and retries would only add duplicate work.
resource "aws_lambda_function_event_invoke_config" "monitor" {
  function_name                = aws_lambda_function.monitor.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 300
}
