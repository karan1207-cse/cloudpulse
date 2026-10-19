# CloudPulse

A cloud reliability and incident-response platform: CloudPulse checks HTTP(S)
endpoints on a schedule, opens an incident after repeated failures, detects
recovery, keeps history and state across restarts, sends notifications, and
shows everything in a small React dashboard.

It runs fully **locally** (JSON files, no AWS account needed) and has an
**AWS** backend (Lambda + EventBridge + DynamoDB + SNS + CloudWatch) defined in
Terraform.

> **Implementation status at a glance**
>
> | Area | Status |
> | --- | --- |
> | Monitoring engine, incidents, JSON storage, CLI, API, dashboard | Implemented and **tested locally** |
> | DynamoDB / SNS adapters, Lambda handler | Implemented and **tested against mocked AWS (moto)** |
> | Terraform | Written; **syntax-parsed locally only**. `terraform fmt -check` and `terraform validate` run in CI — Terraform was not installed on the development machine |
> | AWS deployment | **Not deployed.** No AWS resources have been created; there is no public endpoint |

> 📸 **Screenshot placeholder** — no screenshot is committed yet. Run the
> dashboard locally (see [Dashboard](#dashboard)), capture the overview page,
> save it as `docs/images/dashboard-overview.png` and replace this note with
> `![CloudPulse dashboard](docs/images/dashboard-overview.png)`.

## Contents

- [Features](#features)
- [Architecture](#architecture)
- [Technology stack](#technology-stack)
- [Project structure](#project-structure)
- [Local setup (Windows PowerShell)](#local-setup-windows-powershell)
- [Configuration](#configuration)
- [CLI](#cli)
- [Running the tests](#running-the-tests)
- [Dashboard](#dashboard)
- [AWS architecture](#aws-architecture)
- [Deploying with Terraform](#deploying-with-terraform)
- [Cost and cleanup](#cost-and-cleanup)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Further documentation](#further-documentation)

## Features

- **Health checks** over HTTP/HTTPS with configurable timeout, accepted status
  range (default `200-399`) and redirect limit. Every result records service,
  UTC timestamp, status code, response time and a typed error
  (`DNS_ERROR`, `CONNECTION_ERROR`, `TIMEOUT`, `TLS_ERROR`, `HTTP_STATUS`,
  `INVALID_URL`, `BLOCKED_DESTINATION`, `REDIRECT_LIMIT`, `PROTOCOL_ERROR`,
  `UNEXPECTED_ERROR`).
- **Incident lifecycle** `OPEN → RECOVERED` with severities `LOW`, `MEDIUM`,
  `HIGH`, `CRITICAL`; opens only at the consecutive-failure threshold, never
  duplicates an active incident, resets the counter on success, optional
  escalation to `CRITICAL` after N failures.
- **Restart safe**: active incidents and failure counters are restored;
  missing, corrupt and inconsistent state is handled explicitly.
- **Concurrency safe**: optimistic versioning on monitoring state and
  conditional DynamoDB writes, plus a duplicate-invocation guard.
- **Notifications** on open / escalation / recovery only (not on every failed
  check) via logging and Amazon SNS; delivery failures never affect incident
  state.
- **Storage adapters** with one shared contract: in-memory, JSON files,
  DynamoDB (Query-based, GSIs, TTL, pagination).
- **CLI** for one-off checks, scheduled runs, status, incidents, history,
  serving the dashboard API, and config validation, with meaningful exit codes.
- **Read-only JSON API** + **React/TypeScript dashboard** (overview, services,
  incidents with filters/sorting/pagination, service detail with response-time
  trend and recovery timeline). A clearly labelled demo mode is opt-in.
- **SSRF protection**: loopback, private, link-local and metadata addresses
  are blocked after DNS resolution, on the connected socket, and after every
  redirect.

### What "healthy" means

A check is healthy when, within the timeout, the endpoint returns a final
response (after at most `max_redirects` redirects, each re-validated) whose
status code is inside the service's `accepted_status` range (inclusive).
Everything else is unhealthy. "Recovered" means a later check was healthy;
CloudPulse never claims a root cause or that anything was fixed.

## Architecture

```mermaid
flowchart LR
    subgraph Local
        CLI["cloudpulse CLI<br/>run / monitor / check"] --> Core
        Serve["cloudpulse serve<br/>(127.0.0.1)"] --> API["Read-only JSON API"]
        Dash["React dashboard"] -- "/api (same origin / Vite proxy)" --> Serve
    end
    subgraph AWS
        EB["EventBridge schedule"] --> L["Lambda: cloudpulse.aws_lambda.handler"]
        L --> Core
        L -- "EMF log lines" --> CW["CloudWatch Logs + metrics + alarms"]
        CW -- alarms --> SNS
    end
    Core["MonitoringService<br/>checker → tracker → storage → notifier"]
    Core --> Checker["HttpHealthChecker<br/>+ SSRF guard"]
    Core --> Store{{"Storage protocols"}}
    Store --> JSON["JSON files<br/>data/*.json"]
    Store --> DDB["DynamoDB<br/>history · incidents · state"]
    API --> Store
    Core --> Notify{{"Notifier"}}
    Notify --> Log["Logging"]
    Notify --> SNS["SNS topic → email (optional)"]
```

Details: [docs/architecture.md](docs/architecture.md).

## Technology stack

Python 3.12+ (tested on 3.13 and 3.14; standard library HTTP client, no
runtime dependencies except `boto3` for AWS) · pytest, moto, Ruff · AWS Lambda,
EventBridge, DynamoDB, SNS, CloudWatch · Terraform ≥ 1.10 · React 18 +
TypeScript + Vite, Vitest, Testing Library, ESLint · GitHub Actions.

## Project structure

```text
src/cloudpulse/
  health_check.py         HTTP checker, typed errors, guarded transport
  url_safety.py           URL validation and SSRF policy
  incident_manager.py     Incident model + per-service state machine
  state.py                Persisted per-service monitoring state
  monitor.py              MonitoringService (one service, one check)
  runner.py               Check many services, isolate failures, time budget
  storage.py              Storage protocols
  history.py, json_storage.py, incident_storage.py   in-memory/JSON adapters
  dynamodb_storage.py, dynamodb_incident_storage.py  DynamoDB adapters
  notifications.py        Notifier protocol, logging/SNS/composite notifiers
  config.py               Env + services-file configuration and validation
  factory.py              Wiring of backends
  aws_lambda.py           Lambda entry point
  api.py, api_server.py   Read-only dashboard API and local server
  observability.py        JSON logs and CloudWatch EMF metrics
  cli.py                  Command-line interface
tests/                    pytest suite (unit, integration, mocked AWS)
dashboard/                React + TypeScript dashboard (Vite)
infra/terraform/          AWS infrastructure
docs/                     Architecture, plan, deployment, testing
.github/workflows/        CI (automatic) and deploy (manual only)
```

## Local setup (Windows PowerShell)

Requires Python 3.12+ (3.14 used during development) and, for the
dashboard, Node.js 22.12+ or 24.

```powershell
cd C:\path\to\cloudpulse
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python -m pip install -e .        # provides the `cloudpulse` command
cloudpulse --help
```

If script execution is blocked, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or call
`.\.venv\Scripts\python.exe -m cloudpulse` without activating.

## Configuration

Configuration comes from environment variables; services come from a JSON
file (`CLOUDPULSE_SERVICES_FILE`) or inline JSON (`CLOUDPULSE_SERVICES`).
See [.env.example](.env.example) (placeholders only) and
[services.example.json](services.example.json). CloudPulse does not read
`.env` files itself — set variables in your shell:

```powershell
$env:CLOUDPULSE_SERVICES_FILE = "services.example.json"
cloudpulse config show
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLOUDPULSE_ENV` | `local` | `production` forbids private targets |
| `CLOUDPULSE_BACKEND` | `local` | `local` (JSON), `memory`, `aws` |
| `CLOUDPULSE_DATA_DIR` | `data` | JSON storage directory |
| `CLOUDPULSE_SERVICES_FILE` / `CLOUDPULSE_SERVICES` | — | service list (one of them) |
| `CLOUDPULSE_TIMEOUT_SECONDS` | `5` | default per-check timeout (max 30) |
| `CLOUDPULSE_FAILURE_THRESHOLD` | `3` | default failures before an incident |
| `CLOUDPULSE_MAX_REDIRECTS` | `5` | redirects followed (each re-validated) |
| `CLOUDPULSE_ALLOW_PRIVATE_TARGETS` | `false` | local testing only |
| `CLOUDPULSE_MIN_CHECK_INTERVAL_SECONDS` | `0` | skip duplicate checks |
| `CLOUDPULSE_LOG_LEVEL` | `INFO` | `DEBUG`…`ERROR` |
| `CLOUDPULSE_API_HOST` / `_PORT` | `127.0.0.1` / `8000` | local API |
| `CLOUDPULSE_CORS_ORIGINS` | Vite dev origins | explicit origins, no `*` |
| `AWS_REGION` | — | AWS backend region |
| `CLOUDPULSE_HEALTH_TABLE`, `_INCIDENTS_TABLE`, `_STATE_TABLE` | — | required for `aws` |
| `CLOUDPULSE_SNS_TOPIC_ARN` | — | SNS alerts (aws backend only) |
| `CLOUDPULSE_HISTORY_RETENTION_DAYS` | `30` | DynamoDB TTL for history |

Per-service options: `name` (required), `url`, `timeout_seconds`,
`failure_threshold`, `severity`, `accepted_status` (`[low, high]`),
`escalation_threshold`, `enabled`. Unknown keys are rejected; at most 50
services.

## CLI

```powershell
cloudpulse check https://example.com/            # one check, nothing stored
cloudpulse run                                    # check all configured services once
cloudpulse run --loop --interval 60               # keep checking
cloudpulse run --service example-site --json
cloudpulse status                                 # current state per service
cloudpulse incidents --status OPEN --severity HIGH
cloudpulse history --service example-site --limit 20
cloudpulse serve                                  # API on http://127.0.0.1:8000/api
cloudpulse config validate
cloudpulse https://example.com --threshold 3 --interval 10   # legacy continuous monitor
```

Global options: `--backend {local,memory,aws}`, `--services-file`,
`--data-dir`, `--allow-private`, `--log-level`.

Exit codes: `0` ok/healthy · `1` a check was unhealthy · `2` usage or
configuration error · `3` storage error or a service could not be processed ·
`130` interrupted.

To test against a local endpoint (e.g. `http://127.0.0.1:8080`) pass
`--allow-private` (refused when `CLOUDPULSE_ENV=production`).

## Running the tests

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m ruff format --check src tests
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest --cov
```

Results on the development machine (Windows 11, Python 3.14.0): **357
passed**, branch coverage **94 %**. Dashboard: **18 passed**. Tests never
contact AWS (moto mocks) and only use the network against servers they
start on `127.0.0.1`. See [docs/TESTING.md](docs/TESTING.md).

## Dashboard

```powershell
# terminal 1: API over your local data
cloudpulse serve
# terminal 2: dashboard dev server (proxies /api to 127.0.0.1:8000)
cd dashboard
npm ci
npm run dev        # http://127.0.0.1:5173
```

Or build once and serve both from one process:

```powershell
cd dashboard; npm ci; npm run build; cd ..
cloudpulse serve --static dashboard/dist          # http://127.0.0.1:8000
```

Pages: **Overview** (totals, open incidents, recent recoveries, recent
checks), **Services**, **Incidents** (status/severity filters, sortable
columns, pagination), **Service detail** (check history, response-time trend,
incident history and recovery timeline). Loading, empty and error states are
handled; all times are shown in UTC.

**Demo mode** (`http://127.0.0.1:5173/?demo=1` or `VITE_DEMO_MODE=true`)
uses fabricated sample data and shows a permanent banner saying so. It is
never enabled implicitly.

The browser only talks to the CloudPulse API; it never holds AWS credentials.
To view AWS data, run `cloudpulse serve --backend aws` locally with your own
credentials. No hosted API or dashboard is deployed.

## AWS architecture

- **EventBridge** rule (`rate(5 minutes)` by default) invokes the Lambda;
  a resource policy allows only that rule.
- **Lambda** (`python3.13`, arm64) runs `cloudpulse.aws_lambda.handler`:
  loads and validates config, checks each service with a per-service time
  budget, isolates per-service failures, writes structured JSON logs and EMF
  metrics, and returns a summary. No retries for async invocations.
- **DynamoDB** (on-demand): `…-health-checks` (PK `service`, SK
  `checked_at#id`, TTL), `…-incidents` (PK `id`, GSIs
  `service-created-index` and `status-created-index`), `…-monitor-state`
  (PK `service`, versioned).
- **SNS** topic for incident notifications and alarms; optional email
  subscription (must be confirmed by the recipient — CloudPulse never assumes
  it is confirmed).
- **CloudWatch**: log group with retention, metrics `ChecksPerformed`,
  `ChecksFailed`, `IncidentsOpened`, `IncidentsEscalated`,
  `IncidentsRecovered`, `NotificationFailures`, `ServiceErrors`,
  `ServicesSkipped`; alarms for Lambda errors, duration, heartbeat, service
  errors and notification failures.
- **IAM**: the Lambda role can only write its own logs, use the three tables
  (specific actions) and publish to the one topic; it also carries a
  permissions boundary.

Changing the schedule: set `schedule_expression` (e.g. `"rate(1 minute)"` or
`"cron(0/10 * * * ? *)"`) and keep `min_check_interval_seconds` below the
interval, then `terraform plan` / `apply`.

## Deploying with Terraform

Nothing in this repository has been deployed. Full instructions, IAM
prerequisites, state handling, SNS subscription and verification steps are in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). Short version:

```powershell
cd infra\terraform
Copy-Item terraform.tfvars.example terraform.tfvars   # edit placeholders
terraform init
terraform fmt -check
terraform validate
terraform plan -out tfplan        # review resources and cost first
terraform apply tfplan            # only when you are ready to pay for it
```

## Cost and cleanup

Designed for very low cost at hobby scale: on-demand DynamoDB, short Lambda
runs, no NAT gateway, VPC, API Gateway or KMS keys. Expect cents per month for
a handful of services at a 5-minute schedule, plus ~USD 0.10 per CloudWatch
alarm per month (5 alarms; disable with `enable_alarms = false`) and EMF
custom metrics (8 metrics, ~USD 0.30 each per month after the free tier).
Check current AWS pricing for your region — these are estimates, not
measurements.

Cleanup: `terraform destroy` in `infra/terraform` (review the plan first;
with `enable_deletion_protection = true` disable it first). Locally, delete
the `data/` directory to reset JSON storage.

## Security

Implemented:

- SSRF guard: scheme allow-list, no credentials in URLs, IP-literal check,
  DNS resolution check of **every** address, connect-to-validated-IP and
  connected-peer check (prevents DNS rebinding), redirect re-validation,
  environment proxies ignored. Private targets can be allowed for local
  testing only; `CLOUDPULSE_ENV=production` refuses that.
- Bounded work: per-check timeout (max 30 s per service, budget shared
  across redirects), max 5 redirects by default, response bodies never read,
  max 50 services, bounded API page sizes, bounded JSON history.
- No secrets in code; AWS credentials only via the standard chain. URLs are
  redacted (no user-info, query or fragment) in notifications, the API and
  stored history. Error messages for unexpected exceptions contain only the
  exception type.
- Read-only API, no target registration endpoint, strict query validation,
  explicit CORS allow-list, security headers, binds to `127.0.0.1` and refuses
  other addresses unless `--allow-remote` is given.
- Least-privilege IAM with resource-scoped ARNs and a permissions boundary;
  the optional GitHub deploy role uses OIDC, is restricted to one repository
  and environment, and can only create roles carrying the boundary.
- Optimistic concurrency + conditional writes prevent duplicate incidents
  across concurrent or retried invocations.
- `npm audit`: 0 known vulnerabilities at the pinned versions (checked
  2026-10-19).

Known limitations:

- The API has **no authentication**. It is meant for local use; do not expose
  it publicly. Putting it behind real auth (e.g. an authenticating proxy) is
  future work.
- The timeout is a socket timeout per operation plus a budget across
  redirects; a server that drips headers slowly can exceed it. The Lambda time
  budget still bounds each run.
- JSON storage is for a single local process (atomic writes, no file locks).
- On Windows, connecting to a closed local port can surface as `TIMEOUT`
  rather than `CONNECTION_ERROR` because Windows retries refused connections.
- GitHub Actions are pinned to major version tags, not commit SHAs.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `ModuleNotFoundError: cloudpulse` | `pip install -e .` or run pytest from the repo root (pytest is configured with `pythonpath = src`) |
| `configuration error: … blocked (non-public) address` | Target is loopback/private. For local testing use `--allow-private`; never in production |
| `No services configured` | Set `CLOUDPULSE_SERVICES_FILE` or pass `--services-file` |
| `storage error: … is not valid JSON` | The data file is corrupt. CloudPulse will not overwrite it — fix or move it |
| Dashboard shows "Cannot reach the CloudPulse API" | Start `cloudpulse serve` (port 8000) before `npm run dev` |
| `The aws backend requires: …` | Set the three `CLOUDPULSE_*_TABLE` variables (Terraform outputs them) |
| No alert emails | Confirm the SNS subscription email; check spam |

## Further documentation

- [docs/architecture.md](docs/architecture.md) — components, data model, state machine
- [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) — audit, bugs fixed, phases
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — AWS deployment and teardown
- [docs/TESTING.md](docs/TESTING.md) — test strategy and results
