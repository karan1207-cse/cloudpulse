# Credentials come from the standard AWS chain (SSO profile, environment,
# or the GitHub OIDC role in CI). Never put access keys in this directory.
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.tags
  }
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}
