data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# Least-privilege runtime permissions, scoped to the exact resources.
data "aws_iam_policy_document" "monitor" {
  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.monitor.arn}:*"]
  }

  statement {
    sid       = "HealthHistory"
    actions   = ["dynamodb:PutItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.health_checks.arn]
  }

  statement {
    sid = "Incidents"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
    ]
    resources = [
      aws_dynamodb_table.incidents.arn,
      "${aws_dynamodb_table.incidents.arn}/index/*",
    ]
  }

  statement {
    sid       = "MonitorState"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.monitor_state.arn]
  }

  statement {
    sid       = "PublishAlerts"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }
}

# Permissions boundary: the most any CloudPulse runtime role may ever be
# granted. The optional GitHub deploy role can only create roles that carry
# this boundary, so it cannot escalate privileges through a new role.
data "aws_iam_policy_document" "boundary" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:${local.partition}:logs:${var.aws_region}:${local.account_id}:log-group:/aws/lambda/${local.name_prefix}-*"]
  }

  statement {
    sid = "Tables"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
    ]
    resources = [
      "arn:${local.partition}:dynamodb:${var.aws_region}:${local.account_id}:table/${local.name_prefix}-*",
    ]
  }

  statement {
    sid       = "Alerts"
    actions   = ["sns:Publish"]
    resources = ["arn:${local.partition}:sns:${var.aws_region}:${local.account_id}:${local.name_prefix}-*"]
  }
}

resource "aws_iam_policy" "boundary" {
  name        = "${local.name_prefix}-permissions-boundary"
  description = "Upper bound for CloudPulse runtime roles."
  policy      = data.aws_iam_policy_document.boundary.json
}

resource "aws_iam_role" "monitor" {
  name                 = "${local.name_prefix}-monitor"
  assume_role_policy   = data.aws_iam_policy_document.lambda_assume.json
  permissions_boundary = aws_iam_policy.boundary.arn
}

resource "aws_iam_role_policy" "monitor" {
  name   = "${local.name_prefix}-monitor"
  role   = aws_iam_role.monitor.id
  policy = data.aws_iam_policy_document.monitor.json
}
