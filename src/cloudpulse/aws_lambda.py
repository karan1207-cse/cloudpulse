"""AWS Lambda entry point for scheduled monitoring.

Handler: ``cloudpulse.aws_lambda.handler``. It is deliberately thin — config,
dependency wiring and the run loop all live in ordinary, locally tested
modules.

Accepted events:

* EventBridge scheduled events (``source == "aws.events"``) — check all
  enabled services.
* A manual test event ``{"services": ["name", ...]}`` — check a subset.

Duplicate deliveries (EventBridge is at-least-once) are absorbed by the
per-service ``CLOUDPULSE_MIN_CHECK_INTERVAL_SECONDS`` guard and the
version-checked state writes, so a retried event never opens a second
incident or sends a second notification.
"""

import logging
from typing import Any

from cloudpulse.config import AppConfig, load_config
from cloudpulse.errors import ConfigError
from cloudpulse.factory import Dependencies, build_dependencies
from cloudpulse.observability import configure_logging, emit_metrics
from cloudpulse.runner import run_services

logger = logging.getLogger("cloudpulse.lambda")

_ALLOWED_MANUAL_KEYS = {"services"}
_cache: dict[str, Any] = {}


class InvalidEventError(ValueError):
    """The Lambda event does not have a supported shape."""


def select_services(event: Any, config: AppConfig) -> list:
    """Validate the event and return the services it asks to check."""
    if not isinstance(event, dict):
        raise InvalidEventError("Event must be a JSON object.")
    enabled = list(config.enabled_services)
    if event.get("source") == "aws.events":
        return enabled
    unknown = set(event) - _ALLOWED_MANUAL_KEYS
    if unknown:
        raise InvalidEventError(
            f"Unsupported event keys: {', '.join(sorted(unknown))}."
        )
    requested = event.get("services")
    if requested is None:
        return enabled
    if not isinstance(requested, list) or not all(
        isinstance(n, str) for n in requested
    ):
        raise InvalidEventError('"services" must be a list of service names.')
    by_name = {s.name: s for s in enabled}
    missing = [name for name in requested if name not in by_name]
    if missing:
        raise InvalidEventError(f"Unknown or disabled services: {', '.join(missing)}.")
    return [by_name[name] for name in dict.fromkeys(requested)]


def _dependencies(config: AppConfig) -> Dependencies:
    """Reuse AWS clients across warm invocations."""
    key = repr(config)
    if _cache.get("key") != key:
        _cache["deps"] = build_dependencies(config)
        _cache["key"] = key
    return _cache["deps"]


def handler(event: Any, context: Any) -> dict[str, Any]:
    configure_logging("INFO", json_format=True)
    request_id = getattr(context, "aws_request_id", None)
    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("Invalid configuration: %s", exc, extra={"request_id": request_id})
        raise
    logging.getLogger().setLevel(config.log_level)
    if config.backend != "aws":
        raise ConfigError("Lambda must run with CLOUDPULSE_BACKEND=aws.")

    try:
        services = select_services(event, config)
    except InvalidEventError as exc:
        logger.error("Rejected event: %s", exc, extra={"request_id": request_id})
        return {"ok": False, "error": "invalid_event", "detail": str(exc)}

    deps = _dependencies(config)
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    summary = run_services(services, deps, config, remaining_time_ms=remaining)
    result = summary.to_dict()
    result["ok"] = not summary.failures
    result["request_id"] = request_id

    emit_metrics(summary.metrics(), dimensions={"Environment": config.environment})
    logger.info(
        "Monitoring run complete",
        extra={
            "request_id": request_id,
            "checked": result["checked"],
            "unhealthy": result["unhealthy"],
            "metrics": result["metrics"],
            "failures": result["failures"],
            "skipped_for_time": result["skipped_for_time"],
        },
    )

    if services and summary.failures and len(summary.failures) == len(services):
        # Nothing could be processed (e.g. DynamoDB unavailable): fail the
        # invocation so the Lambda Errors alarm fires.
        raise RuntimeError(f"All {len(services)} service checks failed.")
    return result
