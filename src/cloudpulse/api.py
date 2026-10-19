"""JSON API consumed by the dashboard.

The API is framework-free: :meth:`DashboardApi.handle` maps
``(method, path, query, body, context)`` to ``(status, body)`` and is served
by :mod:`cloudpulse.api_server`. It reads storage server-side, so the browser
never needs AWS credentials.

Read routes (``GET``)::

    /api/health                         capabilities, scheduler status, defaults
    /api/overview
    /api/services
    /api/services/{name}
    /api/services/{name}/checks?limit=
    /api/incidents?status=&severity=&service=&sort=&order=&page=&page_size=
    /api/incidents/{id}

Write routes (only when the request context allows writes — see
:mod:`cloudpulse.api_server` for the local-only / admin-token rules)::

    POST   /api/services                add a service
    PUT    /api/services/{name}         edit a service
    DELETE /api/services/{name}         delete a service
    POST   /api/services/{name}/check   check one service now
    POST   /api/checks                  check all enabled services now
    POST   /api/services/{name}/components            add a page/backend manually
    POST   /api/services/{name}/components/{action}   disable | enable | remove

Every URL is validated with the SSRF policy when saved (including DNS
resolution) and again by the checker on every check and redirect.
"""

import contextlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cloudpulse.clock import Clock, to_utc_iso, utc_now
from cloudpulse.config import MAX_SERVICES, NAME_PATTERN, AppConfig, ServiceConfig
from cloudpulse.errors import (
    ServiceNotFoundError,
    ServiceReadOnlyError,
    ServiceValidationError,
    StorageError,
)
from cloudpulse.incident_manager import SEVERITIES, STATUSES
from cloudpulse.service_registry import ServiceEntry, ServiceRegistry
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
    "check_mode",
    "component_counts",
    "failed_components",
)
WRITE_METHODS = ("POST", "PUT", "DELETE")


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        field: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field
        self.headers = headers or {}

    def body(self) -> dict[str, Any]:
        data: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.field is not None:
            data["field"] = self.field
        return data


@dataclass(frozen=True)
class RequestContext:
    """Per-request permissions decided by the HTTP layer."""

    can_write: bool = False
    auth_required: bool = False


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


def _object_body(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_body", "The request body must be a JSON object.")
    return body


def _valid_name(name: str) -> str:
    if not NAME_PATTERN.match(name):
        raise ApiError(400, "invalid_parameter", "Invalid service name.")
    return name


def _not_allowed(allowed: tuple[str, ...]) -> ApiError:
    return ApiError(
        405,
        "method_not_allowed",
        f"Allowed methods: {', '.join(allowed)}.",
        headers={"Allow": ", ".join(allowed)},
    )


class DashboardApi:
    def __init__(
        self,
        config: AppConfig,
        history: HealthCheckStorage,
        incidents: IncidentStorage,
        clock: Clock = utc_now,
        *,
        registry: ServiceRegistry | None = None,
        runner: Any = None,
        scheduler: Any = None,
    ) -> None:
        self.config = config
        self.history = history
        self.incidents = incidents
        self.clock = clock
        self.registry = registry or ServiceRegistry(config, None)
        self.runner = runner
        self.scheduler = scheduler

    # -- dispatch -----------------------------------------------------------

    def handle(
        self,
        method: str,
        path: str,
        query: Query,
        body: Any = None,
        context: RequestContext | None = None,
    ) -> tuple[int, dict[str, Any]]:
        status, payload, _ = self.handle_full(method, path, query, body, context)
        return status, payload

    def handle_full(
        self,
        method: str,
        path: str,
        query: Query,
        body: Any = None,
        context: RequestContext | None = None,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        """Like :meth:`handle` but also returns extra response headers."""
        context = context or RequestContext()
        try:
            return (
                *self._route(method, path.rstrip("/") or "/", query, body, context),
                {},
            )
        except ApiError as exc:
            return exc.status, exc.body(), exc.headers
        except ServiceValidationError as exc:
            return (
                400,
                ApiError(400, "validation_error", exc.detail, field=exc.field).body(),
                {},
            )
        except ServiceNotFoundError as exc:
            return 404, {"error": "not_found", "message": str(exc)}, {}
        except ServiceReadOnlyError as exc:
            return 409, {"error": "read_only", "message": str(exc)}, {}
        except StorageError as exc:
            logger.error("Storage error serving %s %s: %s", method, path, exc)
            return (
                503,
                {
                    "error": "storage_unavailable",
                    "message": "Monitoring data is temporarily unavailable.",
                },
                {},
            )
        except Exception:
            logger.exception("Unhandled error serving %s %s", method, path)
            return (
                500,
                {
                    "error": "internal_error",
                    "message": "Unexpected server error.",
                },
                {},
            )

    def _require_write(self, context: RequestContext) -> None:
        if context.can_write:
            return
        if context.auth_required:
            raise ApiError(
                401,
                "unauthorized",
                "An admin token is required to make changes on this server.",
                headers={"WWW-Authenticate": 'Bearer realm="cloudpulse"'},
            )
        raise ApiError(
            403,
            "forbidden",
            "Changes can only be made from the computer running CloudPulse.",
        )

    def _route(
        self,
        method: str,
        path: str,
        query: Query,
        body: Any,
        context: RequestContext,
    ) -> tuple[int, dict[str, Any]]:
        parts = [p for p in path.split("/") if p]
        if parts[:1] != ["api"]:
            raise ApiError(404, "not_found", "Unknown endpoint.")
        parts = parts[1:]

        def only(*allowed: str) -> None:
            if method not in allowed:
                raise _not_allowed(allowed)
            if method in WRITE_METHODS:
                self._require_write(context)

        if parts == ["health"]:
            only("GET")
            _reject_unknown(query, set())
            return 200, self.health(context)
        if parts == ["overview"]:
            only("GET")
            _reject_unknown(query, set())
            return 200, self.overview()
        if parts == ["services"]:
            only("GET", "POST")
            _reject_unknown(query, set())
            if method == "POST":
                service, warnings = self.registry.create(_object_body(body))
                return 201, self._saved(service, warnings)
            return 200, {
                "items": [self.service_summary(e) for e in self.registry.entries()]
            }
        if parts == ["checks"]:
            only("POST")
            _reject_unknown(query, set())
            return 200, self.check_all()
        if len(parts) == 2 and parts[0] == "services":
            only("GET", "PUT", "DELETE")
            name = _valid_name(parts[1])
            if method == "PUT":
                _reject_unknown(query, set())
                service, warnings = self.registry.update(name, _object_body(body))
                return 200, self._saved(service, warnings)
            if method == "DELETE":
                _reject_unknown(query, set())
                self.registry.delete(name)
                return 200, {"deleted": name}
            _reject_unknown(query, {"limit"})
            limit = _int_param(query, "limit", 100, 1, MAX_CHECKS)
            return 200, self.service_detail(self._entry(name), limit)
        if len(parts) in (3, 4) and parts[0] == "services" and parts[2] == "components":
            only("POST")
            _reject_unknown(query, set())
            action = parts[3] if len(parts) == 4 else "add"
            if action not in ("add", "disable", "enable", "remove"):
                raise ApiError(404, "not_found", "Unknown endpoint.")
            entry = self._entry(_valid_name(parts[1]))
            return 200, self.change_component(entry, action, _object_body(body))
        if len(parts) == 3 and parts[0] == "services" and parts[2] == "check":
            only("POST")
            _reject_unknown(query, set())
            return 200, self.check_one(self._entry(_valid_name(parts[1])))
        if len(parts) == 3 and parts[0] == "services" and parts[2] == "checks":
            only("GET")
            _reject_unknown(query, {"limit"})
            entry = self._entry(_valid_name(parts[1]))
            limit = _int_param(query, "limit", 50, 1, MAX_CHECKS)
            return 200, {
                "items": [
                    public_check(r)
                    for r in self.history.get_recent(entry.service.name, limit)
                ]
            }
        if parts == ["incidents"]:
            only("GET")
            return 200, self.list_incidents(query)
        if len(parts) == 2 and parts[0] == "incidents":
            only("GET")
            _reject_unknown(query, set())
            if not _ID_PATTERN.match(parts[1]):
                raise ApiError(400, "invalid_parameter", "Invalid incident id.")
            record = self.incidents.get(parts[1])
            if record is None:
                raise ApiError(404, "not_found", "Incident not found.")
            return 200, public_incident(record)
        raise ApiError(404, "not_found", "Unknown endpoint.")

    # -- views --------------------------------------------------------------

    def _entry(self, name: str) -> ServiceEntry:
        entry = self.registry.get(name)
        if entry is None:
            raise ApiError(404, "not_found", f"Service {name!r} is not configured.")
        return entry

    def health(self, context: RequestContext) -> dict[str, Any]:
        scheduler = (
            self.scheduler.status()
            if self.scheduler is not None
            else {"enabled": False}
        )
        return {
            "status": "ok",
            "backend": self.config.backend,
            "environment": self.config.environment,
            "time": to_utc_iso(self.clock()),
            "can_write": context.can_write and self.registry.can_store,
            "can_check": context.can_write and self.runner is not None,
            "auth_required": context.auth_required,
            "service_store": self.registry.can_store,
            "scheduler": scheduler,
            "limits": {
                "max_services": MAX_SERVICES,
                "services": len(self.registry.entries()),
            },
            "defaults": {
                "timeout_seconds": self.config.default_timeout,
                "failure_threshold": self.config.default_failure_threshold,
                "severity": "HIGH",
                "accepted_status": [200, 399],
            },
        }

    def _saved(self, service: ServiceConfig, warnings: list[str]) -> dict[str, Any]:
        entry = self._entry(service.name)
        return {"service": self.service_summary(entry), "warnings": warnings}

    def _require_runner(self) -> Any:
        if self.runner is None:
            raise ApiError(
                503, "checks_unavailable", "Checking is not available on this server."
            )
        return self.runner

    def check_one(self, entry: ServiceEntry) -> dict[str, Any]:
        summary = self._require_runner().check_service(entry.service.name)
        outcome = summary.outcomes[0].to_dict() if summary.outcomes else None
        return {
            "outcome": outcome,
            "failure": summary.failures[0] if summary.failures else None,
            "service": self.service_summary(self._entry(entry.service.name)),
        }

    def check_all(self) -> dict[str, Any]:
        summary = self._require_runner().check_all()
        data = summary.to_dict()
        return {
            "checked": data["checked"],
            "healthy": data["healthy"],
            "unhealthy": data["unhealthy"],
            "failures": data["failures"],
            "services": data["services"],
        }

    def service_summary(self, entry: ServiceEntry) -> dict[str, Any]:
        service = entry.service
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
            "source": entry.source,
            "editable": entry.editable and self.registry.can_store,
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

    def service_detail(self, entry: ServiceEntry, limit: int) -> dict[str, Any]:
        name = entry.service.name
        checks = [public_check(r) for r in self.history.get_recent(name, limit)]
        incidents = [
            public_incident(r)
            for r in self.incidents.list_incidents(service=name, limit=100)
        ]
        return {
            "service": self.service_summary(entry),
            "checks": checks,
            "incidents": incidents,
            "site": self.site_view(entry),
        }

    # -- full-site components ------------------------------------------------

    def site_view(self, entry: ServiceEntry) -> dict[str, Any]:
        from cloudpulse.site_check import SiteLimits, load_profile

        profile = load_profile(self.incidents, entry.service.name)
        components = [dict(c) for c in profile.last_components]
        seen = {c.get("url") for c in components}
        for manual in profile.manual:
            shown = redact_url(manual["url"])
            if shown not in seen:
                pending = "disabled" if shown in profile.disabled else "pending"
                components.append(
                    {
                        "url": shown,
                        "kind": manual["kind"],
                        "source": "manual",
                        "status": pending,
                        "status_code": None,
                        "response_time_ms": None,
                        "error": None,
                        "error_type": None,
                    }
                )
        limits = SiteLimits()
        return {
            "check_mode": entry.service.check_mode,
            "discovered_at": (
                to_utc_iso(profile.discovered_at) if profile.discovered_at else None
            ),
            "last_checked_at": (
                to_utc_iso(profile.last_checked_at) if profile.last_checked_at else None
            ),
            "components": components,
            "limits": {
                "max_manual": limits.max_manual,
                "max_backends": limits.max_backends,
                "max_pages": limits.max_pages,
            },
        }

    def change_component(
        self, entry: ServiceEntry, action: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        from cloudpulse.site_check import SiteProfile

        name = entry.service.name
        raw_url = body.get("url")
        if not isinstance(raw_url, str) or not raw_url.strip():
            raise ApiError(400, "validation_error", "A URL is required.", field="url")
        raw_url = raw_url.strip()
        lock = self.runner.lock if self.runner is not None else contextlib.nullcontext()
        with lock:
            profile = SiteProfile.from_dict(name, self.incidents.get_site_profile(name))
            if action == "add":
                self._add_component(profile, raw_url, body.get("kind", "backend"))
            else:
                self._toggle_component(entry, profile, action, redact_url(raw_url))
            self.incidents.save_site_profile(name, profile.to_dict())
        return {"site": self.site_view(entry)}

    def _add_component(self, profile: Any, raw_url: str, kind: Any) -> None:
        from cloudpulse.site_check import MANUAL_KINDS, SiteLimits

        if kind not in MANUAL_KINDS:
            raise ApiError(
                400,
                "validation_error",
                "Type must be 'backend' or 'page'.",
                field="kind",
            )
        url = self.registry.validate_component_url(raw_url)
        shown = redact_url(url)
        if any(redact_url(m["url"]) == shown for m in profile.manual):
            raise ApiError(
                400, "validation_error", "This URL is already listed.", field="url"
            )
        max_manual = SiteLimits().max_manual
        if len(profile.manual) >= max_manual:
            raise ApiError(
                400,
                "validation_error",
                f"You can add at most {max_manual} components yourself.",
            )
        profile.manual.append({"url": url, "kind": kind})

    def _toggle_component(
        self, entry: ServiceEntry, profile: Any, action: str, shown: str
    ) -> None:
        known = {c.get("url") for c in profile.last_components}
        known |= {redact_url(m["url"]) for m in profile.manual}
        known |= {redact_url(b) for b in profile.backends}
        if shown not in known:
            raise ApiError(404, "not_found", "That component is not listed.")
        if action == "disable":
            if shown == redact_url(entry.service.url):
                raise ApiError(
                    400,
                    "validation_error",
                    "The main page is always checked.",
                    field="url",
                )
            if shown not in profile.disabled:
                profile.disabled.append(shown)
            self._set_snapshot_status(profile, shown, "disabled")
        elif action == "enable":
            profile.disabled = [u for u in profile.disabled if u != shown]
            self._set_snapshot_status(profile, shown, "pending")
        else:  # remove: only components the user added
            before = len(profile.manual)
            profile.manual = [
                m for m in profile.manual if redact_url(m["url"]) != shown
            ]
            if len(profile.manual) == before:
                raise ApiError(
                    400,
                    "validation_error",
                    "Only components you added yourself can be removed; turn "
                    "discovered ones off instead.",
                    field="url",
                )
            profile.disabled = [u for u in profile.disabled if u != shown]
            profile.last_components = [
                c for c in profile.last_components if c.get("url") != shown
            ]

    @staticmethod
    def _set_snapshot_status(profile: Any, shown: str, status: str) -> None:
        """Reflect a user change in the last-check snapshot right away."""
        for component in profile.last_components:
            if component.get("url") == shown:
                component.update(
                    status=status,
                    status_code=None,
                    response_time_ms=None,
                    error=None,
                    error_type=None,
                )

    def overview(self) -> dict[str, Any]:
        entries = self.registry.entries()
        names = {e.service.name for e in entries}
        summaries = [self.service_summary(e) for e in entries]
        recent_checks: list[dict[str, Any]] = []
        for entry in entries:
            recent_checks.extend(self.history.get_recent(entry.service.name, 10))
        recent_checks.sort(key=lambda r: r.get("checked_at") or "", reverse=True)
        # Incidents of services that were deleted stay in the incident list
        # but are not counted as currently open on the overview.
        open_incidents = [
            r
            for r in self.incidents.list_incidents(
                status="OPEN", limit=INCIDENT_SCAN_CAP
            )
            if (r.get("service_name") or r.get("service_url")) in names
        ]
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
