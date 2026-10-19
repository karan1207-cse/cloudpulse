resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "${local.name_prefix}-schedule"
  description         = "Trigger CloudPulse health checks"
  schedule_expression = var.schedule_expression
}

resource "aws_cloudwatch_event_target" "monitor" {
  rule      = aws_cloudwatch_event_rule.schedule.name
  target_id = "cloudpulse-monitor"
  arn       = aws_lambda_function.monitor.arn
}

# Only this rule may invoke the function.
resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridgeSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.monitor.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}
