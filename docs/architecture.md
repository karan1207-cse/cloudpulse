# CloudPulse — Initial Architecture

## 1. Purpose

CloudPulse is a cloud reliability and incident response platform that monitors application health, detects failures, tracks incidents, and identifies when services recover.

## 2. Planned Architecture

1. **Scheduler:** Amazon EventBridge periodically triggers health checks.
2. **Health Monitor:** AWS Lambda checks target endpoints and measures response time.
3. **Incident Manager:** Evaluates failures, severity, deduplication, and recovery.
4. **Storage:** Amazon DynamoDB stores monitoring results and incident history.
5. **Alerting:** Amazon SNS sends notifications about important incidents.
6. **Dashboard:** React and TypeScript display service health and incident history.
7. **Infrastructure:** Terraform defines cloud resources and GitHub Actions automates testing.

## 3. Development Approach

The first version will be developed and tested locally before AWS integration. Core monitoring and incident logic will remain separate from cloud-specific services where practical.

## 4. Initial Monitoring Results

Each health check will eventually record:

- Target URL
- Check timestamp
- Success or failure
- HTTP status code, when available
- Response time
- Error details, when applicable

## 5. Incident Lifecycle

The planned lifecycle is:

`HEALTHY → INCIDENT_OPEN → RECOVERED`

Repeated failures should not create duplicate active incidents for the same service.

## 6. Security and Reliability

- Never commit credentials or secret keys.
- Set timeouts for outbound health checks.
- Validate target URLs and handle network errors.
- Add automated tests for failures, recovery, and duplicate incidents.
- Review AWS costs and permissions before deployment.

## 7. Current Status

Milestone 1: Project foundation — in progress.
