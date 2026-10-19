# Testing CloudPulse

## Commands (Windows PowerShell)

```powershell
# Python
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m ruff format --check src tests
.\.venv\Scripts\python.exe -m pytest                 # all tests
.\.venv\Scripts\python.exe -m pytest --cov           # with branch coverage
.\.venv\Scripts\python.exe -m pytest -m aws          # mocked-AWS tests only
.\.venv\Scripts\python.exe -m pytest -m integration  # multi-component tests

# Dashboard
cd dashboard
npm ci
npm run lint
npm run typecheck
npm test
npm run build
npm audit
```

## Results (2026-10-19, Windows 11, Python 3.14.0, Node 24.19.0)

| Check | Result |
| --- | --- |
| Baseline before changes: `pytest` | 15 collection errors (`ModuleNotFoundError`) |
| Baseline with `PYTHONPATH=src` | 46 passed |
| `ruff check src tests` | All checks passed |
| `ruff format --check src tests` | 55 files already formatted |
| `pytest` | **357 passed**, 0 failed, 0 skipped (~55 s; moto set-up dominates) |
| `pytest --cov` | **94 %** branch coverage of `src/cloudpulse` |
| Dashboard `npm run lint` / `typecheck` | clean |
| Dashboard `npm test` | **18 passed** |
| Dashboard `npm run build` | OK (≈167 kB JS, 53 kB gzip) |
| `npm audit` | 0 vulnerabilities |
| Terraform | not run locally (not installed); HCL parsed with `python-hcl2` and variable/local references cross-checked; `fmt -check` + `validate` run in CI |

Manual end-to-end check performed: `cloudpulse run --allow-private` against a
local `http.server` and a closed port opened and tracked an incident;
`cloudpulse serve --static dashboard/dist` served the built dashboard, which
showed the real totals, the open incident, the check history and the
response-time trend in the browser.

## What is covered

| Area | Files | Highlights |
| --- | --- | --- |
| URL validation / SSRF | `test_url_safety.py` | schemes, credentials, ports, IPv4/IPv6 private/loopback/link-local/metadata/mapped addresses, DNS results with one private address, redaction |
| HTTP checker | `test_health_check_engine.py`, `test_health_check.py` | accepted ranges, HTTP errors, DNS/connect/timeout/TLS/protocol errors, redirect following and re-validation, redirect limit, timeout budget, real sockets on 127.0.0.1 (blocked by default, allowed explicitly), proxy env ignored |
| Incidents | `test_incident_transitions.py`, `test_incident_detection.py`, `test_incident_manager.py`, `test_incident_restore.py` | every transition, escalation once, thresholds, counter reset, serialization round-trips, corrupt records |
| Storage contract | `test_storage_contract.py` | same suite on memory, JSON and DynamoDB (moto): ordering, filters, duplicates, versioned state, state never counted as incidents |
| DynamoDB specifics | `test_dynamodb_adapters_moto.py`, `test_dynamodb_*storage.py` | sort key + TTL, Query pagination across pages, GSI-only listing (no scans), conditional writes, conflicts, AWS error mapping, region not hard-coded, Decimal conversion |
| JSON safety | `test_json_storage_safety.py`, `test_json_storage.py`, `test_incident_storage.py` | corrupt files never overwritten, legacy state format, atomic writes, bounded history |
| Monitoring flows | `test_monitor_integration.py`, `test_monitor*.py` | threshold→open→recover with one notification each, restart (JSON and DynamoDB), concurrent workers, duplicate invocations, inconsistent state repair, notification/storage failures |
| Notifications | `test_notifiers.py`, `test_notifications.py` | secrets never in messages, SNS subject limits, SNS via moto, error wrapping, composite and swallow-on-failure |
| Config | `test_config.py` | every validation rule, production refuses private targets, example file valid |
| CLI | `test_cli_commands.py`, `test_cli.py` | each command, exit codes 0/1/2/3/130, legacy form |
| API | `test_api.py` | every route, validation errors, 503/500 without leaks, real HTTP server: security headers, CORS allow-list, preflight, 405s, static files, path traversal |
| Lambda | `test_aws_lambda.py` | scheduled event end-to-end with moto DynamoDB + SNS, duplicate delivery, recovery across cold starts, event validation, time budget, SNS failure, partial and total storage failure, SSRF via DNS in Lambda, warm-start reuse, EMF shape |
| Dashboard | `dashboard/src/test/dashboard.test.tsx` | API client (query strings, errors, timeouts), demo mode opt-in + banner, overview, loading/error/retry, services table, incident filters/sort/pagination, detail page chart + timeline, 404, routing |

## Rules the suite follows

- No live AWS: `conftest.py` sets dummy credentials, all AWS tests use
  `moto.mock_aws()`.
- No external network: checks use scripted transports or servers bound to
  `127.0.0.1` in the test itself.
- Deterministic time: `FixedClock` is injected wherever timestamps matter.
- Regression tests accompany every fixed bug (search for "Regression" in
  `tests/`).

## Changed pre-existing tests

All 46 original tests still pass. Formatting was normalised by `ruff format`
(no assertion changes). Three assertions in
`tests/test_dynamodb_incident_storage.py` (an untracked file at the start)
were **strengthened**, because they pinned the old read-modify-write update
that loses concurrent changes:

- `test_storage_adds_incident` now also requires the
  `attribute_not_exists(id)` condition.
- `test_storage_updates_existing_incident` now requires a single conditional
  `UpdateItem` and no `put_item`.
- `test_storage_returns_false_for_missing_incident` now drives the
  `ConditionalCheckFailedException` path.
