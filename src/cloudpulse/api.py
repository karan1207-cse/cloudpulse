"""Read-only JSON API consumed by the dashboard.

The API is framework-free: :meth:`DashboardApi.handle` maps
``(method, path, query)`` to ``(status, body)`` and is served by
:mod:`cloudpulse.api_server`. It reads storage server-side, so the browser
never needs AWS credentials. There are no write endpoints — monitored
services come only from configuration, so the API cannot be used to register
arbitrary targets.

Routes (all ``GET``)::

    /api/health
    /api/overview
    /api/services
    /api/services/{name}
    /api/services/{name}/checks?limit=
    /api/incidents?status=&severity=&service=&sort=&order=&page=&page_size=
    /api/incidents/{id}
"""

import logging
import re
from collections.abc import Mapping
from typing import Any

from cloudpulse.clock import Clock, to_utc_iso, utc_now
from cloudpulse.config import NAME_PATTERN, AppConfig, ServiceConfig
from cloudpulse.errors import StorageError
from cloudpulse.incident_manager import SEVERITIES, STATUSES
from cloudpulse.storage import HealthCheckStorage, IncidentStorage
from cloudpulse.url_safety import redact_url

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
MAX_CHECKS = 500
INCIDENT_SCAN_CAP = 1000
SORT_FIELDS = ("created_at", "recovered_at", "severity", "service_name", "status")
_SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}
_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_PUBLIC_CHECK_FIELDS = (
    "id",
    "service",
    "url",
    "final_url",
    "checked_at",
    "is_healthy",
    "status_code",
    "response_time_ms",
    "error",
    "error_type",
    "redirects",
)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


Query = Mapping[str, list[str]]


def _one(query: Query, key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    if len(values) > 1:
        raise ApiError(400, "invalid_parameter", f"'{key}' may only be given once.")
    return values[0]


def _int_param(query: Query, key: str, default: int, minimum: int, maximum: int) -> int:
    raw = _one(query, key)
    if raw is None or raw == "":
        return default
    if not raw.isdigit():
        raise ApiError(400, "invalid_parameter", f"'{key}' must be a positive integer.")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ApiError(
            400,
            "invalid_parameter",
            f"'{key}' must be between {minimum} and {maximum}.",
        )
    return value


def _enum_param(query: Query, key: str, choices: tuple[str, ...]) -> str | None:
    raw = _one(query, key)
    if raw is None or raw == "":
        return None
    value = raw.upper() if key in ("status", "severity") else raw
    if value not in choices:
        raise ApiError(
            400, "invalid_parameter", f"'{key}' must be one of {', '.join(choices)}."
        )
    return value


def _reject_unknown(query: Query, allowed: set[str]) -> None:
    unknown = set(query) - allowed
    if unknown:
        raise ApiError(
            400,
            "invalid_parameter",
            f"Unknown parameter(s): {', '.join(sorted(unknown))}.",
        )


def public_check(record: dict[str, Any]) -> dict[str, Any]:
    """Return only documented fields of a check, with URLs redacted."""
    data = {key: record.get(key) for key in _PUBLIC_CHECK_FIELDS if key in record}
    data.setdefault("service", record.get("url"))
    for key in ("url", "final_url"):
        if data.get(key):
            data[key] = redact_url(str(data[key]))
    return data


def public_incident(record: dict[str, Any]) -> dict[str, Any]:
    data = dict(record)
    data.pop("expires_at", None)
    if data.get("service_url"):
        data["service_url"] = redact_url(str(data["service_url"]))
    data.setdefault("service_name", record.get("service_url"))
    return data


class DashboardApi:
    def __init__(
        self,
        config: AppConfig,
        history: HealthCheckStorage,
        incidents: IncidentStorage,
        clock: Clock = utc_now,
    ) -> None:
        self.config = config
        self.history = history
        self.incidents = incidents
        self.clock = clock

    # -- dispatch -----------------------------------------------------------

    def handle(
        self, method: str, path: str, query: Query
    ) -> tuple[int, dict[str, Any]]:
        try:
            if method != "GET":
                raise ApiError(405, "method_not_allowed", "Only GET is supported.")
            return 200, self._route(path.rstrip("/") or "/", query)
        except ApiError as exc:
            return exc.status, {"error": exc.code, "message": exc.message}
        except StorageError as exc:
            logger.error("Storage error serving %s: %s", path, exc)
            return 503, {
                "error": "storage_unavailable",
                "message": "Monitoring data is temporarily unavailable.",
            }
        except Exception:
            logger.exception("Unhandled error serving %s", path)
            return 500, {
                "error": "internal_error",
                "message": "Unexpected server error.",
            }

    def _route(self, path: str, query: Query) -> dict[str, Any]:
        parts = [p for p in path.split("/") if p]
        if parts[:1] != ["api"]:
            raise ApiError(404, "not_found", "Unknown endpoint.")
        parts = parts[1:]
        if parts == ["health"]:
            _reject_unknown(query, set())
            return {
                "status": "ok",
                "backend": self.config.backend,
                "environment": self.config.environment,
                "time": to_utc_iso(self.clock()),
            }
        if parts == ["overview"]:
            _reject_unknown(query, set())
            return self.overview()
        if parts == ["services"]:
            _reject_unknown(query, set())
            return {"items": [self.service_summary(s) for s in self.config.services]}
        if len(parts) == 2 and parts[0] == "services":
            _reject_unknown(query, {"limit"})
            limit = _int_param(query, "limit", 100, 1, MAX_CHECKS)
            return self.service_detail(self._service(parts[1]), limit)
        if len(parts) == 3 and parts[0] == "services" and parts[2] == "checks":
            _reject_unknown(query, {"limit"})
            service = self._service(parts[1])
            limit = _int_param(query, "limit", 50, 1, MAX_CHECKS)
            return {
                "items": [
                    public_check(r)
                    for r in self.history.get_recent(service.name, limit)
                ]
            }
        if parts == ["incidents"]:
            return self.list_incidents(query)
        if len(parts) == 2 and parts[0] == "incidents":
            _reject_unknown(query, set())
            if not _ID_PATTERN.match(parts[1]):
                raise ApiError(400, "invalid_parameter", "Invalid incident id.")
            record = self.incidents.get(parts[1])
            if record is None:
                raise ApiError(404, "not_found", "Incident not found.")
            return public_incident(record)
        raise ApiError(404, "not_found", "Unknown endpoint.")

    # -- views --------------------------------------------------------------

    def _service(self, name: str) -> ServiceConfig:
        if not NAME_PATTERN.match(name):
            raise ApiError(400, "invalid_parameter", "Invalid service name.")
        service = self.config.service(name)
        if service is None:
            raise ApiError(404, "not_found", f"Service {name!r} is not configured.")
        return service

    def service_summary(self, service: ServiceConfig) -> dict[str, Any]:
        state = self.incidents.get_state(service.name)
        incident = None
        if state and state.active_incident_id:
            record = self.incidents.get(state.active_incident_id)
            if record and record.get("status") == "OPEN":
                incident = {
                    "id": record["id"],
                    "severity": record.get("severity"),
                    "created_at": record.get("created_at"),
                }
        if state is None or state.last_checked_at is None:
            status = "unknown"
        elif incident is not None or not state.last_is_healthy:
            status = "unhealthy"
        else:
            status = "healthy"
        return {
            **service.to_public_dict(),
            "status": status,
            "last_checked_at": (
                to_utc_iso(state.last_checked_at)
                if state and state.last_checked_at
                else None
            ),
            "last_status_code": state.last_status_code if state else None,
            "last_response_time_ms": state.last_response_time_ms if state else None,
            "last_error": state.last_error if state else None,
            "last_error_type": state.last_error_type if state else None,
            "consecutive_failures": state.consecutive_failures if state else 0,
            "current_incident": incident,
        }

    def service_detail(self, service: ServiceConfig, limit: int) -> dict[str, Any]:
        checks = [public_check(r) for r in self.history.get_recent(service.name, limit)]
        incidents = [
            public_incident(r)
            for r in self.incidents.list_incidents(service=service.name, limit=100)
        ]
        return {
            "service": self.service_summary(service),
            "checks": checks,
            "incidents": incidents,
        }

    def overview(self) -> dict[str, Any]:
        summaries = [self.service_summary(s) for s in self.config.services]
        recent_checks: list[dict[str, Any]] = []
        for service in self.config.services:
            recent_checks.extend(self.history.get_recent(service.name, 10))
        recent_checks.sort(key=lambda r: r.get("checked_at") or "", reverse=True)
        open_incidents = self.incidents.list_incidents(
            status="OPEN", limit=INCIDENT_SCAN_CAP
        )
        recoveries = self.incidents.list_incidents(status="RECOVERED", limit=50)
        recoveries.sort(key=lambda r: r.get("recovered_at") or "", reverse=True)
        return {
            "generated_at": to_utc_iso(self.clock()),
            "totals": {
                "services": len(summaries),
                "healthy": sum(1 for s in summaries if s["status"] == "healthy"),
                "unhealthy": sum(1 for s in summaries if s["status"] == "unhealthy"),
                "unknown": sum(1 for s in summaries if s["status"] == "unknown"),
                "open_incidents": len(open_incidents),
            },
            "open_incidents": [public_incident(r) for r in open_incidents[:10]],
            "recent_recoveries": [public_incident(r) for r in recoveries[:5]],
            "recent_checks": [public_check(r) for r in recent_checks[:10]],
        }

    def list_incidents(self, query: Query) -> dict[str, Any]:
        _reject_unknown(
            query,
            {"status", "severity", "service", "sort", "order", "page", "page_size"},
        )
        status = _enum_param(query, "status", STATUSES)
        severity = _enum_param(query, "severity", SEVERITIES)
        sort = _enum_param(query, "sort", SORT_FIELDS) or "created_at"
        order = _enum_param(query, "order", ("asc", "desc")) or "desc"
        page = _int_param(query, "page", 1, 1, 10_000)
        page_size = _int_param(query, "page_size", 20, 1, MAX_PAGE_SIZE)
        service = _one(query, "service") or None
        if service is not None and not NAME_PATTERN.match(service):
            raise ApiError(400, "invalid_parameter", "Invalid service name.")

        records = self.incidents.list_incidents(
            status=status, service=service, limit=INCIDENT_SCAN_CAP
        )
        truncated = len(records) >= INCIDENT_SCAN_CAP
        if severity is not None:
            records = [r for r in records if r.get("severity") == severity]

        def sort_key(record: dict[str, Any]) -> Any:
            if sort == "severity":
                return (
                    _SEVERITY_RANK.get(record.get("severity", ""), -1),
                    record.get("created_at") or "",
                )
            return (record.get(sort) or "", record.get("created_at") or "")

        records.sort(key=sort_key, reverse=order == "desc")
        total = len(records)
        start = (page - 1) * page_size
        return {
            "items": [public_incident(r) for r in records[start : start + page_size]],
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": max(1, -(-total // page_size)),
            "truncated": truncated,
        }
