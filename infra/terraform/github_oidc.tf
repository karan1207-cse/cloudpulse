# Optional: a role that the *manual* GitHub Actions deploy workflow assumes via
# OIDC, so no long-lived AWS keys are stored in GitHub. Created only when
# var.github_repository is set. Bootstrap it once with a local `terraform
# apply` by an administrator; afterwards CI can plan/apply the rest.
#
# Scope: only resources named "<project>-<environment>-*", only from the given
# repository AND GitHub environment, and any IAM role it creates must carry
# the CloudPulse permissions boundary (prevents privilege escalation).

locals {
  github_enabled     = var.github_repository != null
  github_count       = local.github_enabled ? 1 : 0
  github_oidc_url    = "token.actions.githubusercontent.com"
  github_oidc_arn    = local.github_enabled ? (var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "arn:${local.partition}:iam::${local.account_id}:oidc-provider/${local.github_oidc_url}") : null
  resource_arn_scope = "${var.aws_region}:${local.account_id}"
}

resource "aws_iam_openid_connect_provider" "github" {
  count = local.github_enabled && var.create_github_oidc_provider ? 1 : 0

  url             = "https://${local.github_oidc_url}"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1", "1c58a3a8518e8759bf075b76b750d4f2df264fcd"]
}

data "aws_iam_policy_document" "github_assume" {
  count = local.github_count

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_url}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_url}:sub"
      values   = ["repo:${coalesce(var.github_repository, "none/none")}:environment:${var.github_environment}"]
    }
  }
}

data "aws_iam_policy_document" "github_deploy" {
  count = local.github_count

  statement {
    sid       = "TerraformStateList"
    actions   = ["s3:ListBucket"]
    resources = ["arn:${local.partition}:s3:::${coalesce(var.terraform_state_bucket, "unset")}"]
  }

  statement {
    sid       = "TerraformStateObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:${local.partition}:s3:::${coalesce(var.terraform_state_bucket, "unset")}/${var.project_name}/*"]
  }

  statement {
    sid = "DynamoDBTables"
    actions = [
      "dynamodb:CreateTable",
      "dynamodb:DeleteTable",
      "dynamodb:DescribeTable",
      "dynamodb:UpdateTable",
      "dynamodb:DescribeTimeToLive",
      "dynamodb:UpdateTimeToLive",
      "dynamodb:DescribeContinuousBackups",
      "dynamodb:UpdateContinuousBackups",
      "dynamodb:TagResource",
      "dynamodb:UntagResource",
      "dynamodb:ListTagsOfResource",
    ]
    resources = ["arn:${local.partition}:dynamodb:${local.resource_arn_scope}:table/${local.name_prefix}-*"]
  }

  statement {
    sid       = "LambdaFunction"
    actions   = ["lambda:*"]
    resources = ["arn:${local.partition}:lambda:${local.resource_arn_scope}:function:${local.name_prefix}-*"]
  }

  statement {
    sid       = "PassMonitorRoleToLambdaOnly"
    actions   = ["iam:PassRole"]
    resources = ["arn:${local.partition}:iam::${local.account_id}:role/${local.name_prefix}-monitor"]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["lambda.amazonaws.com"]
    }
  }

  statement {
    sid       = "EventBridgeRule"
    actions   = ["events:*"]
    resources = ["arn:${local.partition}:events:${local.resource_arn_scope}:rule/${local.name_prefix}-*"]
  }

  statement {
    sid       = "SnsTopic"
    actions   = ["sns:*"]
    resources = ["arn:${local.partition}:sns:${local.resource_arn_scope}:${local.name_prefix}-*"]
  }

  statement {
    sid = "LogGroup"
    actions = [
      "logs:CreateLogGroup",
      "logs:DeleteLogGroup",
      "logs:PutRetentionPolicy",
      "logs:DeleteRetentionPolicy",
      "logs:TagResource",
      "logs:UntagResource",
      "logs:ListTagsForResource",
      "logs:ListTagsLogGroup",
    ]
    resources = ["arn:${local.partition}:logs:${local.resource_arn_scope}:log-group:/aws/lambda/${local.name_prefix}-*"]
  }

  statement {
    sid       = "AlarmsWrite"
    actions   = ["cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms", "cloudwatch:TagResource", "cloudwatch:UntagResource", "cloudwatch:ListTagsForResource"]
    resources = ["arn:${local.partition}:cloudwatch:${local.resource_arn_scope}:alarm:${local.name_prefix}-*"]
  }

  statement {
    sid       = "DescribeOnlyActions"
    actions   = ["cloudwatch:DescribeAlarms", "logs:DescribeLogGroups"]
    resources = ["*"]
  }

  statement {
    sid = "ManageMonitorRole"
    actions = [
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
      "iam:DeleteRole",
      "iam:DeleteRolePolicy",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:UpdateAssumeRolePolicy",
    ]
    resources = ["arn:${local.partition}:iam::${local.account_id}:role/${local.name_prefix}-monitor"]
  }

  statement {
    sid       = "CreateRolesOnlyWithBoundary"
    actions   = ["iam:CreateRole", "iam:PutRolePolicy", "iam:PutRolePermissionsBoundary"]
    resources = ["arn:${local.partition}:iam::${local.account_id}:role/${local.name_prefix}-monitor"]

    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [aws_iam_policy.boundary.arn]
    }
  }

  statement {
    sid = "ReadBootstrapResources"
    actions = [
      "iam:GetPolicy",
      "iam:GetPolicyVersion",
      "iam:ListPolicyVersions",
      "iam:GetOpenIDConnectProvider",
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
    ]
    resources = [
      aws_iam_policy.boundary.arn,
      local.github_oidc_arn,
      "arn:${local.partition}:iam::${local.account_id}:role/${local.name_prefix}-github-deploy",
    ]
  }
}

resource "aws_iam_role" "github_deploy" {
  count = local.github_count

  name                 = "${local.name_prefix}-github-deploy"
  description          = "Assumed by the manual GitHub Actions deploy workflow via OIDC."
  assume_role_policy   = data.aws_iam_policy_document.github_assume[0].json
  max_session_duration = 3600

  lifecycle {
    precondition {
      condition     = var.terraform_state_bucket != null
      error_message = "terraform_state_bucket is required when github_repository is set."
    }
  }
}

resource "aws_iam_role_policy" "github_deploy" {
  count = local.github_count

  name   = "${local.name_prefix}-github-deploy"
  role   = aws_iam_role.github_deploy[0].id
  policy = data.aws_iam_policy_document.github_deploy[0].json
}
