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
| `pytest` | **529 passed**, 0 failed, 0 skipped (~95 s; moto set-up dominates) |
| `pytest --cov` | **92 %** branch coverage of `src/cloudpulse` |
| Dashboard `npm run lint` / `typecheck` | clean |
| Dashboard `npm test` | **67 passed** |
| Dashboard `npm run build` | OK (≈195 kB JS, 61 kB gzip) |
| `npm audit` | 0 vulnerabilities |
| Terraform | not run locally (not installed); HCL parsed with `python-hcl2` and variable/local references cross-checked; `fmt -check` + `validate` run in CI |

Manual full-site check (2026-10-20, GET only) of
`https://expense-tracker-feb75.web.app/`: the page (200), its JS bundle and
CSS were up, and the only backend found in the bundle was
`axios.create({baseURL:"http://localhost:5000/api"})`. CloudPulse reported the
site **Unhealthy** — "Backend http://localhost:5000/ points to a
local/private address, so visitors' browsers cannot reach it" — without
contacting localhost. This matches the reported "Quick Add" failure: the
deployed build calls a development API URL. Adding the site through the
dashboard (Full site, the default) produced the same result and showed the
components under *What we check*.

Manual end-to-end check of service management (port 8820, fresh data
directory): the empty dashboard showed the first-run call to action; adding
`localtest.me` (a public DNS name resolving to 127.0.0.1) was refused by the
server with the private-address message; adding `example.com` saved it, ran a
real first check (HTTP 200) and showed the success notification; background
checks ran every 15 s; `cloudpulse run --loop` refused to start while `serve`
held the scheduler lease; a cross-site `Origin` and a foreign `Host` header
were rejected (403 / 421); editing the threshold and deleting with
confirmation worked, also at 375 px width.

Earlier manual end-to-end check: `cloudpulse run --allow-private` against a
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
| Service list | `test_service_store.py` | store contract on memory/JSON/DynamoDB, corrupt records, registry merge, field-specific validation, DNS-to-private rejection, DNS-failure warning, duplicates, 50 limit, read-only services-file entries, import |
| Write API | `test_api_write.py` | create/update/delete/check-now/check-all, validation and SSRF errors with `field`, 401/403 permission matrix, admin token (constant time, weak tokens rejected, never in repr), real server: loopback writes, cross-site block, Host check, 415/400/413, preflight, rate limits |
| Full-site checks | `test_site_check.py`, `test_api_components.py` | HTML analysis of a Vite/React-shaped SPA, bundle scanning (onrender.com API found; fonts/react.dev/sentry/googleapis/w3.org filtered), backend patterns (axios baseURL, API_URL, VITE_*, fetch), GET-only polite requests without cookies, 404/401/405 = up, frontend 200 + backend timeout = unhealthy naming the backend, 5xx/missing asset/broken page, private backend never contacted, DNS-to-private blocked, redirect to metadata blocked, caps (pages/assets/backends/bytes/time), discovery cache + rediscovery (new bundle, 6 h, Check now), turn off / manual components, persistence on memory/JSON/DynamoDB, incident + notification wording, compact history, API add/disable/enable/remove with SSRF validation |
| Scheduler | `test_scheduler.py` | lease exclusivity/expiry/renewal/release on all backends, cross-process JSON lease, concurrent acquire has one winner, background ticks, skipping recently checked services, start/stop, `run --loop` refusal, `services` CLI |
| Dashboard | `dashboard/src/test/dashboard.test.tsx`, `services.test.tsx`, `site.test.tsx` | API client (query strings, errors, timeouts), demo mode opt-in + banner, overview, loading/error/retry, services table, incident filters/sort/pagination, detail page chart + timeline, 404, routing |

## Rules the suite follows

- No live AWS: `conftest.py` sets dummy credentials, all AWS tests use
  `moto.mock_aws()`.
- No external network: checks use scripted transports or servers bound to
  `127.0.0.1` in the test itself.
- Deterministic time: `FixedClock` is injected wherever timestamps matter.
- Regression tests accompany every fixed bug (search for "Regression" in
  `tests/`).

## Changed pre-existing tests (service-management feature)

- `tests/test_api.py::test_non_get_rejected` became
  `test_unsupported_methods_rejected`: `POST /api/services` is now a real,
  permission-checked route (403 without permission), so 405 is asserted on
  routes that really have no POST.
- Dashboard "shows a loading state, then an error with retry" now expects the
  new first-run call to action instead of the old "No services are
  configured" text.

## Changed pre-existing tests (initial upgrade)

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
