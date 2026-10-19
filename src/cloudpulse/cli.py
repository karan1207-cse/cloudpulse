"""Command-line interface for CloudPulse.

Exit codes:
    0  success / all checked services healthy
    1  at least one check was unhealthy
    2  usage or configuration error
    3  storage error (unavailable, corrupt) or a service could not be processed
  130  interrupted (Ctrl+C) during a one-shot command

The legacy form ``cloudpulse <url> [--threshold N] [--interval S]`` still
works and is equivalent to ``cloudpulse monitor <url> ...``.
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from cloudpulse.config import BACKENDS, AppConfig, load_config
from cloudpulse.errors import ConfigError, StorageError
from cloudpulse.factory import build_dependencies, build_storage
from cloudpulse.health_check import HttpHealthChecker
from cloudpulse.incident_manager import SEVERITIES, STATUSES
from cloudpulse.incident_storage import JsonIncidentStorage
from cloudpulse.json_storage import JsonHealthCheckStorage
from cloudpulse.monitor import MonitoringService
from cloudpulse.observability import configure_logging
from cloudpulse.url_safety import redact_url

EXIT_OK = 0
EXIT_UNHEALTHY = 1
EXIT_USAGE = 2
EXIT_STORAGE = 3
EXIT_INTERRUPTED = 130

COMMANDS = (
    "check",
    "run",
    "monitor",
    "status",
    "incidents",
    "history",
    "serve",
    "config",
)


def _status_range(value: str) -> tuple[int, int]:
    try:
        low, _, high = value.partition("-")
        result = (int(low), int(high or low))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use LOW-HIGH, e.g. 200-399") from exc
    if not 100 <= result[0] <= result[1] <= 599:
        raise argparse.ArgumentTypeError("range must satisfy 100 <= LOW <= HIGH <= 599")
    return result


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--backend",
        choices=BACKENDS,
        help="storage backend (default: CLOUDPULSE_BACKEND or local)",
    )
    common.add_argument(
        "--services-file",
        type=Path,
        help="JSON file listing services (default: CLOUDPULSE_SERVICES_FILE)",
    )
    common.add_argument(
        "--data-dir", type=Path, help="directory for local JSON storage (default: data)"
    )
    common.add_argument(
        "--allow-private",
        action="store_true",
        help=(
            "allow loopback/private targets for local testing "
            "(refused when CLOUDPULSE_ENV=production)"
        ),
    )
    common.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="log verbosity",
    )

    parser = argparse.ArgumentParser(
        prog="cloudpulse",
        description="CloudPulse: health checks, incident detection and recovery.",
        epilog="Exit codes: 0 ok, 1 unhealthy, 2 usage/config error, 3 storage error.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser(
        "check", parents=[common], help="check one URL once (nothing is stored)"
    )
    p.add_argument("url", help="HTTP or HTTPS URL")
    p.add_argument(
        "--timeout", type=_positive_float, default=5.0, help="seconds (default 5)"
    )
    p.add_argument(
        "--accepted-status",
        type=_status_range,
        default=(200, 399),
        metavar="LOW-HIGH",
        help="healthy status range (default 200-399)",
    )
    p.add_argument("--json", action="store_true", help="print JSON")

    p = sub.add_parser(
        "run", parents=[common], help="check configured services and persist results"
    )
    p.add_argument(
        "--service",
        action="append",
        metavar="NAME",
        help="only this service (repeatable)",
    )
    p.add_argument("--loop", action="store_true", help="repeat until Ctrl+C")
    p.add_argument(
        "--interval",
        type=_positive_float,
        default=60.0,
        help="seconds between rounds with --loop (default 60)",
    )
    p.add_argument("--json", action="store_true", help="print the run summary as JSON")

    p = sub.add_parser(
        "monitor", parents=[common], help="continuously monitor a single URL"
    )
    p.add_argument("url", help="HTTP or HTTPS URL to monitor")
    p.add_argument(
        "--threshold",
        type=int,
        default=3,
        help="consecutive failures required to open an incident",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=10.0,
        help="seconds to wait between health checks",
    )
    p.add_argument(
        "--timeout",
        type=_positive_float,
        default=5.0,
        help="request timeout in seconds",
    )

    p = sub.add_parser(
        "status", parents=[common], help="show the current state of configured services"
    )
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("incidents", parents=[common], help="list incidents")
    p.add_argument("--status", choices=STATUSES, type=str.upper)
    p.add_argument("--severity", choices=SEVERITIES, type=str.upper)
    p.add_argument("--service", metavar="NAME")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("history", parents=[common], help="show recent health checks")
    p.add_argument(
        "--service", metavar="NAME", help="service name (default: all configured)"
    )
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser(
        "serve", parents=[common], help="serve the dashboard API on localhost"
    )
    p.add_argument("--host", help="bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, help="port (default 8000)")
    p.add_argument(
        "--static",
        type=Path,
        metavar="DIR",
        help="also serve a built dashboard (e.g. dashboard/dist)",
    )
    p.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow binding to a non-loopback address (no auth!)",
    )

    p = sub.add_parser(
        "config", parents=[common], help="validate and print configuration"
    )
    p.add_argument(
        "action", choices=("validate", "show"), nargs="?", default="validate"
    )
    return parser


def _load(args: argparse.Namespace) -> AppConfig:
    import os

    env = dict(os.environ)
    if getattr(args, "data_dir", None):
        env["CLOUDPULSE_DATA_DIR"] = str(args.data_dir)
    if getattr(args, "log_level", None):
        env["CLOUDPULSE_LOG_LEVEL"] = args.log_level
    overrides: dict[str, Any] = {}
    if getattr(args, "backend", None):
        overrides["backend"] = args.backend
    if getattr(args, "allow_private", False):
        overrides["allow_private_targets"] = True
    return load_config(
        env,
        services_file=getattr(args, "services_file", None),
        overrides=overrides,
        # One-off checks and single-URL monitoring do not use the service list,
        # so a problem in it must not block them.
        load_services=args.command not in ("check", "monitor"),
    )


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def _table(rows: list[list[Any]], headers: list[str]) -> None:
    cells = [headers] + [["" if c is None else str(c) for c in row] for row in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    for index, row in enumerate(cells):
        print("  ".join(value.ljust(widths[i]) for i, value in enumerate(row)).rstrip())
        if index == 0:
            print("  ".join("-" * w for w in widths))


# -- commands -----------------------------------------------------------------


def cmd_check(args: argparse.Namespace, config: AppConfig) -> int:
    checker = HttpHealthChecker(
        policy=config.url_policy, max_redirects=config.max_redirects
    )
    result = checker.check(
        args.url,
        service=redact_url(args.url),
        timeout=args.timeout,
        accepted_status=args.accepted_status,
    )
    if args.json:
        _print_json(result)
    else:
        state = "HEALTHY" if result["is_healthy"] else "UNHEALTHY"
        print(f"{state}  {result['url']}")
        print(
            f"  status: {result['status_code']}  "
            f"time: {result['response_time_ms']} ms  at: {result['checked_at']}"
        )
        if result["error"]:
            print(f"  error: [{result['error_type']}] {result['error']}")
    return EXIT_OK if result["is_healthy"] else EXIT_UNHEALTHY


def _selected_services(args: argparse.Namespace, config: AppConfig) -> list:
    if not config.services:
        raise ConfigError(
            "No services configured. Set CLOUDPULSE_SERVICES_FILE or pass "
            "--services-file "
            "(see services.example.json)."
        )
    if not getattr(args, "service", None):
        return list(config.enabled_services)
    selected = []
    for name in args.service:
        service = config.service(name)
        if service is None:
            raise ConfigError(f"Unknown service: {name}")
        selected.append(service)
    return selected


def cmd_run(args: argparse.Namespace, config: AppConfig) -> int:
    from cloudpulse.runner import run_services

    services = _selected_services(args, config)
    deps = build_dependencies(config)
    round_number = 0
    while True:
        round_number += 1
        summary = run_services(services, deps, config)
        if args.json:
            _print_json(summary.to_dict())
        else:
            print(f"Round {round_number} at {summary.started_at}")
            rows = []
            for outcome in summary.outcomes:
                data = outcome.to_dict()
                health = (
                    "skipped"
                    if outcome.skipped
                    else ("healthy" if data["is_healthy"] else "UNHEALTHY")
                )
                rows.append(
                    [
                        outcome.service,
                        health,
                        data["status_code"],
                        data["response_time_ms"],
                        data["error_type"],
                        data["transition"],
                    ]
                )
            for failure in summary.failures:
                rows.append(
                    [failure["service"], "ERROR", None, None, failure["error"], None]
                )
            _table(rows, ["SERVICE", "HEALTH", "STATUS", "MS", "ERROR", "TRANSITION"])
        if not args.loop:
            break
        time.sleep(args.interval)
    if summary.failures:
        return EXIT_STORAGE
    return EXIT_UNHEALTHY if summary.unhealthy else EXIT_OK


def cmd_monitor(args: argparse.Namespace, config: AppConfig) -> int:
    if args.interval <= 0:
        raise ConfigError("--interval must be greater than zero")
    if args.threshold < 1:
        raise ConfigError("--threshold must be at least 1")
    config.url_policy.check_url(args.url)
    deps_checker = HttpHealthChecker(
        policy=config.url_policy, max_redirects=config.max_redirects
    )
    service = MonitoringService(
        service_url=args.url,
        failure_threshold=args.threshold,
        timeout=args.timeout,
        history=JsonHealthCheckStorage(config.data_dir / "health_checks.json"),
        incident_storage=JsonIncidentStorage(config.data_dir / "incidents.json"),
        checker=deps_checker,
    )
    for warning in service.restore_warnings:
        print(f"Warning: {warning}")

    print(f"Monitoring: {args.url}")
    print(f"Check interval: {args.interval} seconds")
    print("Press Ctrl+C to stop.\n")

    check_number = 0
    try:
        while True:
            check_number += 1
            result = service.run_check()
            print(f"\n--- Health Check #{check_number} ---")
            print(json.dumps(result, indent=2))
            print(f"Stored checks: {service.history.count()}")
            for notification in service.notifications:
                print(notification)
            service.notifications.clear()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nCloudPulse monitoring stopped.")
    return EXIT_OK


def cmd_status(args: argparse.Namespace, config: AppConfig) -> int:
    from cloudpulse.api import DashboardApi

    history, incidents = build_storage(config)
    api = DashboardApi(config, history, incidents)
    summaries = [api.service_summary(s) for s in _selected_services(args, config)]
    if args.json:
        _print_json(summaries)
    else:
        _table(
            [
                [
                    s["name"],
                    s["status"],
                    s["last_checked_at"],
                    s["last_status_code"],
                    s["last_response_time_ms"],
                    s["consecutive_failures"],
                    (s["current_incident"] or {}).get("id"),
                ]
                for s in summaries
            ],
            [
                "SERVICE",
                "STATUS",
                "LAST CHECK (UTC)",
                "HTTP",
                "MS",
                "FAILS",
                "OPEN INCIDENT",
            ],
        )
    return EXIT_OK


def cmd_incidents(args: argparse.Namespace, config: AppConfig) -> int:
    if not 1 <= args.limit <= 1000:
        raise ConfigError("--limit must be between 1 and 1000")
    _, incidents = build_storage(config)
    records = incidents.list_incidents(
        status=args.status, service=args.service, limit=1000
    )
    if args.severity:
        records = [r for r in records if r.get("severity") == args.severity]
    records = records[: args.limit]
    if args.json:
        _print_json(records)
    elif not records:
        print("No incidents found.")
    else:
        _table(
            [
                [
                    r.get("id"),
                    r.get("service_name") or r.get("service_url"),
                    r.get("severity"),
                    r.get("status"),
                    r.get("created_at"),
                    r.get("recovered_at"),
                    r.get("last_error_type"),
                ]
                for r in records
            ],
            [
                "ID",
                "SERVICE",
                "SEVERITY",
                "STATUS",
                "CREATED (UTC)",
                "RECOVERED (UTC)",
                "LAST ERROR",
            ],
        )
    return EXIT_OK


def cmd_history(args: argparse.Namespace, config: AppConfig) -> int:
    if not 1 <= args.limit <= 1000:
        raise ConfigError("--limit must be between 1 and 1000")
    history, _ = build_storage(config)
    names = (
        [args.service]
        if args.service
        else [s.name for s in _selected_services(args, config)]
    )
    records: list[dict[str, Any]] = []
    for name in names:
        records.extend(history.get_recent(name, args.limit))
    records.sort(key=lambda r: r.get("checked_at") or "", reverse=True)
    records = records[: args.limit]
    if args.json:
        _print_json(records)
    elif not records:
        print("No health checks recorded.")
    else:
        _table(
            [
                [
                    r.get("checked_at"),
                    r.get("service"),
                    "yes" if r.get("is_healthy") else "NO",
                    r.get("status_code"),
                    r.get("response_time_ms"),
                    r.get("error_type"),
                ]
                for r in records
            ],
            ["CHECKED (UTC)", "SERVICE", "HEALTHY", "HTTP", "MS", "ERROR"],
        )
    return EXIT_OK


def cmd_serve(args: argparse.Namespace, config: AppConfig) -> int:
    from cloudpulse.api import DashboardApi
    from cloudpulse.api_server import create_server

    history, incidents = build_storage(config)
    api = DashboardApi(config, history, incidents)
    host = args.host or config.api_host
    port = args.port or config.api_port
    try:
        server = create_server(
            api,
            host,
            port,
            allowed_origins=config.cors_origins,
            static_dir=args.static,
            allow_remote=args.allow_remote,
        )
    except (ValueError, OSError) as exc:
        raise ConfigError(str(exc)) from exc
    print(
        f"CloudPulse API listening on http://{host}:{port}/api "
        f"(backend: {config.backend})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()
    return EXIT_OK


def cmd_config(args: argparse.Namespace, config: AppConfig) -> int:
    summary = {
        "environment": config.environment,
        "backend": config.backend,
        "data_dir": str(config.data_dir),
        "allow_private_targets": config.allow_private_targets,
        "aws_region": config.aws_region,
        "tables": {
            "health": config.health_table,
            "incidents": config.incidents_table,
            "state": config.state_table,
        },
        "sns_topic_configured": bool(config.sns_topic_arn),
        "services": [s.to_public_dict() for s in config.services],
    }
    if args.action == "show":
        _print_json(summary)
    else:
        print(
            f"Configuration OK: {len(config.services)} service(s), "
            f"backend={config.backend}, environment={config.environment}"
        )
    return EXIT_OK


HANDLERS = {
    "check": cmd_check,
    "run": cmd_run,
    "monitor": cmd_monitor,
    "status": cmd_status,
    "incidents": cmd_incidents,
    "history": cmd_history,
    "serve": cmd_serve,
    "config": cmd_config,
}


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv.insert(0, "monitor")  # legacy: cloudpulse <url> ...

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = _load(args)
        configure_logging(config.log_level)
        logging.getLogger("cloudpulse").debug("Loaded config for %s", args.command)
        return HANDLERS[args.command](args, config)
    except ConfigError as exc:
        print(f"cloudpulse: configuration error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ValueError as exc:
        print(f"cloudpulse: error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except StorageError as exc:
        print(f"cloudpulse: storage error: {exc}", file=sys.stderr)
        return EXIT_STORAGE
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
