# Deploying CloudPulse to AWS

> **Status:** nothing has been deployed. The Terraform configuration has been
> syntax-checked locally (HCL parser) but `terraform fmt -check` and
> `terraform validate` have only been set up to run in CI, because Terraform
> was not installed on the development machine. Run them yourself before
> deploying. `terraform apply` creates billable resources — review the plan
> and costs first.

## 1. Prerequisites

- Terraform ≥ 1.10 (`winget install Hashicorp.Terraform` on Windows).
- AWS CLI v2, signed in with short-lived credentials, e.g.
  `aws configure sso` then `aws sso login --profile <profile>` and
  `$env:AWS_PROFILE = "<profile>"`. Never create long-lived access keys for
  this.
- An identity allowed to create: DynamoDB tables, a Lambda function, IAM
  roles and policies (including a permissions boundary), EventBridge rules,
  an SNS topic and subscription, CloudWatch log groups and alarms, and
  (optionally) a GitHub OIDC provider and role. The scoped policy in
  `infra/terraform/github_oidc.tf` lists the exact actions.
- (Recommended) an S3 bucket for remote state: private, versioned,
  encrypted.

## 2. Configure

```powershell
cd infra\terraform
Copy-Item terraform.tfvars.example terraform.tfvars    # git-ignored
notepad terraform.tfvars
```

Set `aws_region` and `environment`. Services can be added later from the
dashboard (stored in the `<prefix>-services` table); optionally pin some in
`services_json` (public URLs only — the Lambda runs with
`CLOUDPULSE_ENV=production`, so loopback, private, link-local and metadata
destinations are always blocked). Optional: `alert_email`,
`schedule_expression`, `enable_alarms`, `log_retention_days`,
`history_retention_days`, `enable_point_in_time_recovery`,
`enable_deletion_protection`, `reserved_concurrency`.

### State

By default state is local (`terraform.tfstate`, git-ignored). For anything
shared, copy `backend.tf.example` to `backend.tf`, fill in the bucket and
region, and run `terraform init -migrate-state`. State can contain resource
attributes such as the `services_json` value — keep the bucket private.

## 3. Validate and plan

```powershell
terraform init
terraform fmt -check -recursive
terraform validate
terraform plan -out tfplan
```

Expected resources: 4 DynamoDB tables (history, incidents, state, services), 1 Lambda function + async invoke
config + permission, 1 log group, 1 IAM role + inline policy, 1 managed
policy (permissions boundary), 1 EventBridge rule + target, 1 SNS topic +
policy (+ email subscription), up to 5 CloudWatch alarms, and the optional
GitHub OIDC provider/role/policy. No VPC, NAT gateway, API Gateway or KMS key.

## 4. Apply (manual decision)

```powershell
terraform apply tfplan
terraform output
```

## 5. SNS email subscription

If `alert_email` is set, AWS sends a confirmation email. **Alerts are not
delivered until the recipient clicks the confirmation link.** Check status:

```powershell
aws sns list-subscriptions-by-topic --topic-arn (terraform output -raw sns_topic_arn)
```

`PendingConfirmation` in `SubscriptionArn` means it is not confirmed yet.

## 6. Verify

```powershell
$fn = terraform output -raw lambda_function_name
# Manual run of one service (does not wait for the schedule):
aws lambda invoke --function-name $fn --cli-binary-format raw-in-base64-out `
  --payload '{"services":["example-site"]}' out.json; Get-Content out.json
# Logs:
aws logs tail (terraform output -raw lambda_log_group) --since 15m
# Stored data via the local API/dashboard, using your own credentials:
$env:CLOUDPULSE_BACKEND = "aws"
$env:AWS_REGION = "<region>"
$env:CLOUDPULSE_HEALTH_TABLE = terraform output -raw health_table_name
$env:CLOUDPULSE_INCIDENTS_TABLE = terraform output -raw incidents_table_name
$env:CLOUDPULSE_STATE_TABLE = terraform output -raw state_table_name
$env:CLOUDPULSE_SERVICES_TABLE = terraform output -raw services_table_name
cloudpulse services list
cloudpulse serve --static dashboard/dist      # add services in the dashboard
```

With the `aws` backend, `serve` does not run background checks by default
(the scheduled Lambda does); services you add are picked up by the Lambda on
its next run, and **Check now** runs a check from your machine. Your own
identity needs `dynamodb:GetItem`, `Query`, `Scan`, `PutItem`, `UpdateItem`
and `DeleteItem` on the tables (and indexes).

## 7. Changing the schedule

Edit `schedule_expression` (`rate(1 minute)`, `rate(15 minutes)`,
`cron(0/10 * * * ? *)` …) and keep `min_check_interval_seconds` safely below
the interval (it is the duplicate-delivery guard) and
`heartbeat_period_seconds` above it. Then `terraform plan` / `apply`.

## 8. CI/CD

- `.github/workflows/ci.yml` runs on every push/PR: Ruff, pytest (Python
  3.13 and 3.14), dashboard lint/typecheck/tests/build/audit, and Terraform
  fmt/validate (`-backend=false`, no AWS credentials).
- `.github/workflows/deploy.yml` runs **only** via *Run workflow*. It needs:
  1. `github_repository = "<owner>/<repo>"` and `terraform_state_bucket` in
     `terraform.tfvars`, applied once by an administrator (creates the OIDC
     provider and the `<prefix>-github-deploy` role);
  2. a GitHub environment `production` with required reviewers;
  3. repository variable `AWS_REGION`, secrets `AWS_DEPLOY_ROLE_ARN`
     (`terraform output github_deploy_role_arn`), `TF_STATE_BUCKET` and
     `TF_VARS` (contents of `terraform.tfvars`).
  It runs tests, `terraform plan`, and applies only when the `apply` input is
  ticked. The deploy role cannot modify the permissions boundary, its own
  role, or create roles without the boundary; changes to those bootstrap
  resources must be applied by an administrator. If the GitHub OIDC provider
  already exists in the account set `create_github_oidc_provider = false`.

## 9. Teardown

```powershell
cd infra\terraform
terraform plan -destroy -out destroy.tfplan   # review
terraform apply destroy.tfplan
```

If `enable_deletion_protection = true`, set it to `false` and apply first.
Destroying deletes all stored history and incidents.

## 10. Cost notes

All estimates — confirm with the AWS pricing pages for your region. DynamoDB
on-demand and Lambda costs are negligible for a few services every 5 minutes
(well inside the free tier for many accounts). Fixed costs: ~USD 0.10 per
alarm-month (5 alarms) and ~USD 0.30 per custom metric-month for the 8 EMF
metrics beyond the free tier. CloudWatch Logs ingestion/storage depends on
volume; retention defaults to 14 days.
