# CloudPulse — Implementation Plan

This plan was written after auditing the repository on 2026-10-19 and is kept
up to date as the work lands. The source code is the source of truth; this
document records what was found, what was decided, and how it is verified.

## 1. Audit baseline (before any change)

| Item | Finding |
| --- | --- |
| Branch | `main` (tracks `origin/main`), 5 commits, no other branches |
| Uncommitted work | `docs/architecture.md`, `requirements.txt`, `monitor.py`, `storage.py` modified; DynamoDB adapters + tests and `data/` untracked |
| `pytest` (no env) | **15 collection errors** — `ModuleNotFoundError: cloudpulse` (no `pyproject.toml`, no `pythonpath` setting) |
| `pytest` with `PYTHONPATH=src` | **46 passed** |
| `ruff check .` | All checks passed (default rules only, no config) |
| Terraform | Not installed on this machine |
| Node / npm | v24.19.0 / 11.17.0 |
| Frontend, Terraform, CI | None existed |

### Existing functionality

- `health_check.check_health(url, timeout)` — urllib GET, healthy if status
  200–399, catches network errors.
- `incident_manager` — `Incident` dataclass (OPEN/RECOVERED, LOW–CRITICAL) and
  `ServiceIncidentTracker` (threshold, single active incident, reset on success,
  restore after restart).
- `MonitoringService` — runs a check, stores history, opens/recovers incidents,
  persists the failure counter, collects notification strings.
- JSON storage for health checks and incidents (+ a sidecar state file for
  failure counts); in-memory history; early DynamoDB adapters (scan based).
- `cli.py` — one continuous-monitoring command for a single URL.

### Bugs and gaps found

1. **Tests cannot be collected without manual `PYTHONPATH`** (no packaging).
2. **`requirements.txt` is corrupted**: the file is UTF‑16 LE with a BOM and
   `boto3` was appended as ASCII without a newline, so pip cannot parse it
   reliably.
3. `DynamoDBIncidentStorage` does not satisfy `IncidentStorage`: no
   `get_failure_count` / `save_failure_count`, and `get_open_incident` returns a
   raw dict instead of an `Incident` (would crash `restore_active_incident`).
4. DynamoDB reads use full-table `scan` on every check (`get_open_incident`,
   `count`), and `update` is a read‑modify‑write `put_item` (lost-update race).
5. DynamoDB adapters hard-code region `ap-south-1` and import boto3 at module
   import time (local mode would need boto3 installed).
6. Health-check records have **no timestamp and no service name**; history
   cannot be queried per service or by time.
7. Incidents have no `updated_at`, failure info or diagnostics.
8. `Incident.created_at` / `recovered_at` use the wall clock directly — not
   injectable, not deterministic in tests.
9. **SSRF**: any URL (loopback, private ranges, `169.254.169.254`) is fetched,
   redirects are followed blindly by urllib, and environment proxies are used.
10. JSON writes are not atomic (a crash mid-write corrupts the file); a corrupt
    file raises an unhandled `JSONDecodeError`.
11. Notification "delivery" is only string formatting; there is no notifier
    interface, and a failing notifier would propagate into incident handling.
12. CLI: one command only, `ValueError` from storage is turned into a usage
    error, no exit-code semantics, writes to `data/` relative to cwd.
13. No config validation, no `.env.example`, no Lambda handlers, no
    Terraform, no CI, no dashboard, README describes only "planned" features.
14. Mixed line endings (CRLF/LF) and trailing whitespace in some modules.

## 2. Design decisions

- **Keep the existing public API working.** `check_health`, `Incident`,
  `ServiceIncidentTracker`, `MonitoringService(service_url=…)`,
  `JsonIncidentStorage`, `JsonHealthCheckStorage`, `HealthCheckHistory`, the
  DynamoDB class names and the legacy `cloudpulse <url> --interval N` CLI form
  all keep working, so every pre-existing test still passes unmodified.
- **Service identity**: a service has a `name` (key) and a `url`. When only a
  URL is given (legacy usage), the name defaults to the URL.
- **Dependency injection**: `MonitoringService` receives a checker (HTTP
  client), history storage, incident/state storage, clock and notifier.
- **Explicit transitions**: `ServiceIncidentTracker.evaluate()` returns a
  `Transition` (`NONE`, `FAILURE_RECORDED`, `INCIDENT_OPENED`,
  `INCIDENT_ONGOING`, `INCIDENT_ESCALATED`, `INCIDENT_RECOVERED`).
- **Healthy** = the final response (after at most `max_redirects` validated
  redirects) arrives within the timeout with a status inside the configured
  accepted range (default `200–399`). Anything else is unhealthy with a typed
  `error_type`.
- **Storage**: one `HealthCheckStorage` and one `IncidentStorage` protocol
  (incidents + per-service monitoring state). Three DynamoDB tables (history,
  incidents, state) so incident counts can never include state records.
  Optimistic concurrency on state (`version` attribute, conditional write);
  incidents created with `attribute_not_exists`; updates via `UpdateItem`.
- **Notifications**: `Notifier` protocol with logging, SNS, composite and
  in-memory implementations. Delivery failures are caught, logged and reported
  in the check summary — incident state is persisted *before* notifying.
- **Dashboard API**: a dependency-free, read-only JSON API (`cloudpulse serve`)
  that the React dashboard calls. It reads storage server-side, so no AWS
  credentials ever reach the browser. It is **not** deployed to AWS by
  Terraform (no public endpoint is created). Services come from configuration;
  there is no target-registration endpoint.
- **Metrics**: CloudWatch Embedded Metric Format in Lambda logs (no
  `PutMetricData` permission or extra API calls).

### Order changes and why

The requested order was followed, except that **config + URL safety were done
together with the monitoring engine** (the checker needs the policy and limits
to be testable), and the **API was built before the dashboard** because the
dashboard must talk to a real backend.

## 3. Phases and acceptance criteria

| Phase | Acceptance criteria | Status |
| --- | --- | --- |
| 0 Packaging | `pytest` and `ruff` run from a clean checkout with no env vars; pinned dev deps | Done |
| 1 Monitoring engine | typed errors (DNS, connect, timeout, TLS, invalid URL, blocked, HTTP status, redirect); UTC `checked_at`; accepted status range; injectable transport & clock | Done |
| 2 Incidents | threshold open, no duplicates, counter reset, recover, escalate, restore after restart, corrupt/inconsistent state handled | Done |
| 3 Storage | JSON + DynamoDB adapters satisfy the same protocols (shared contract tests run against both); pagination; GSIs; conditional writes | Done |
| 4 Notifications | logging + SNS notifiers; failures isolated; sanitized messages; no re-notify per failed check | Done |
| 5 CLI + config | `check`, `run`, `monitor`, `incidents`, `history`, `status`, `serve`, `config`; exit codes; env + JSON file validation | Done |
| 6 AWS handlers | thin Lambda handler, time-budget aware, partial failures, structured logs, EMF metrics, duplicate-invocation guard; moto tests | Done (mock-tested) |
| 7 API + dashboard | read-only API with validation/tests; React+TS dashboard with 4 pages, filters, sorting, pagination, demo mode clearly labelled | Done |
| 8 Terraform + CI | tables/GSIs, Lambda, IAM, EventBridge, SNS, logs, alarms, outputs; CI for Python, frontend, Terraform; manual deploy workflow with OIDC | Written and HCL-parsed; **`fmt -check`/`validate` not run locally** (Terraform not installed) — they run in CI. **Not deployed.** |
| 9 Security + docs | SSRF guard with DNS + connected-peer validation, redirect re-validation, docs | Done |
| 11 Full-site checks | Per-service check mode (Full site default in the form); discovery of assets/pages/backends from the main URL; GET-only, SSRF-guarded, bounded probing; plain-language failing component in incidents/notifications; *What we check* with turn off / manual add; honest limitations | Done (mock-tested; manually verified against the user's site) |
| 10 Service management | Add/edit/delete URLs and check now from the dashboard; background checks in `serve`; one service list for CLI, API and Lambda (JSON/DynamoDB store, services file still supported); local-only or admin-token writes; CSRF/DNS-rebinding/size/rate limits | Done (DynamoDB store mock-tested; not deployed) |

## 4. Bugs fixed (each has a regression test)

| # | Bug | Fix | Test |
| --- | --- | --- | --- |
| 1 | Tests not collectable without `PYTHONPATH` | `pyproject.toml` with `pythonpath = ["src"]`, package metadata, console script | whole suite |
| 2 | `requirements.txt` corrupted (UTF-16 + ASCII mix) | Rewritten as ASCII; dev deps in `requirements-dev.txt` | CI install |
| 3 | DynamoDB incident adapter missing failure-count methods; `get_open_incident` returned a dict (crash on restore) | Full protocol implemented, returns `Incident` | `test_storage_contract.py`, `test_monitor_integration.py::test_restart_mid_incident_does_not_duplicate[dynamodb]` |
| 4 | DynamoDB full-table scans on every check; read-modify-write `update` | GSIs + `Query`; conditional `UpdateItem` | `test_incident_listing_uses_indexes_not_scans`, `test_storage_updates_existing_incident` |
| 5 | Region hard-coded to `ap-south-1`; boto3 imported at module import | Standard region chain; lazy boto3 | `test_region_is_not_hardcoded` |
| 6 | Health checks had no timestamp/service | `checked_at`, `service`, `id`, `error_type` on every result | `test_result_has_timestamp_service_and_id`, `test_results_always_carry_service_and_timestamp` |
| 7 | SSRF: private/metadata targets fetched, redirects followed blindly, env proxies honoured | `UrlPolicy` + guarded transport + manual, re-validated redirects | `test_url_safety.py`, `test_redirect_to_metadata_endpoint_is_blocked`, `test_localhost_name_is_blocked_after_dns_resolution`, `test_environment_proxy_is_ignored` |
| 8 | Non-atomic JSON writes; corrupt file crashed with raw `JSONDecodeError` | Atomic writes; `StorageCorruptError`, file left untouched | `test_corrupt_history_file_raises_and_is_not_overwritten` |
| 9 | Notification failure could break incident handling; no notifier interface | `Notifier` protocol; state persisted before delivery; failures captured | `test_notification_failure_does_not_corrupt_state`, `test_sns_failure_does_not_lose_incident` |
| 10 | Two workers could open duplicate incidents | Versioned state + conditional writes | `test_concurrent_workers_open_only_one_incident`, `test_concurrent_state_writers_conflict` |
| 11 | Wall-clock timestamps not injectable | Clock injection everywhere | `FixedClock` tests |
| 12 | Equal timestamps returned oldest history record first | Ties broken by insertion order | `test_recent_ordering_breaks_timestamp_ties_by_insertion` |
| 13 | State `last_checked_at` differed from the check's own timestamp | State uses the check timestamp | `test_state_timestamp_matches_check_timestamp` |
| 14 | CLI turned storage `ValueError` into usage errors; no exit codes | Typed errors → exit codes 0/1/2/3/130 | `test_cli_commands.py` |
| 15 | `cloudpulse check <url>` failed when the unrelated services file was invalid | One-off commands skip loading services | `test_check_ignores_unrelated_services_file` |
| 16 | On Windows, writing a JSON file failed ("Access is denied") while another thread read it — triggered by the API and scheduler sharing `serve` | Retry `os.replace` briefly on sharing violations | `test_write_retries_transient_windows_sharing_violation`, `test_concurrent_acquire_has_single_winner` |
| 18 | A deployed SPA showed Healthy while its backend was unreachable (static hosting answers 200) | Full-site check mode | `test_frontend_ok_but_backend_timeout_is_unhealthy`, `test_discovered_private_backend_is_never_contacted_and_counts_as_down` |
| 19 | `localhost`/`*.localhost` URLs were only blocked after DNS resolution | Blocked at the URL check (RFC 6761) | `test_policy_blocks_ip_literals[http://localhost:5000/api]` |
| 20 | Removing a manual component left it visible until the next check | Update the component snapshot on remove/disable/enable | `test_add_manual_backend_then_disable_enable_remove` |
| 17 | Dark-mode contrast: white text on light red for CRITICAL badges and the delete confirmation | Use the background colour for text on red | manual check |

## 5. Remaining work / not done

- Run `terraform fmt -check` and `terraform validate` locally (Terraform not
  installed here); CI is configured to do it.
- No AWS deployment has been made; nothing has been verified against real
  AWS services.
- Reads are unauthenticated (by design for localhost); a single admin token
  protects writes — there are no user accounts.
- Pin GitHub Actions to commit SHAs.
- Optional: a deployed, authenticated read API (e.g. API Gateway + Cognito)
  if the dashboard must be hosted.

## 6. Test coverage plan

- Unit: URL validation/SSRF, checker outcomes (mocked transport), tracker
  transitions, serialization round-trips, JSON storage corruption, DynamoDB
  adapters (moto + MagicMock for error paths), notifier formatting/failures,
  config validation, CLI exit codes.
- Integration: check→incident→recovery flow, restart with persisted state,
  storage contract tests parametrized over JSON and DynamoDB, API routes over
  a populated store, local HTTP server round trip against a `127.0.0.1` server
  (with private targets explicitly allowed).
- AWS (mocked): Lambda handler with moto DynamoDB + SNS, event validation,
  AWS error handling, time budget.
- Frontend: Vitest + Testing Library for API client, filters, pages and
  states.

See [TESTING.md](TESTING.md) for commands and exact counts.

## 7. Security considerations

See README "Security" and [architecture.md](architecture.md). Summary: SSRF
guard (scheme allow-list, no credentials in URLs, DNS resolution check,
connected-peer check, redirect re-validation, proxies disabled), bounded
timeouts and redirects, no response bodies stored or sent, sanitized URLs in
notifications (query strings and user-info removed), least-privilege IAM with
resource-scoped ARNs, no secrets in code, read-only API bound to `127.0.0.1`
by default with an explicit CORS allow-list.

## 8. Deployment prerequisites (not done — require the user)

- Terraform ≥ 1.6 installed locally (or use the CI workflow).
- An AWS account, a remote state bucket (recommended), and credentials via the
  standard chain (SSO / profile / OIDC role) — never committed.
- An SNS email address to subscribe (the subscription must be confirmed by
  the recipient; CloudPulse never assumes it is).
- Review of `terraform plan` and cost before any `apply`.
