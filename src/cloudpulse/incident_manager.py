"""Incident model and the per-service incident state machine.

State machine (per service)::

    HEALTHY --failure--> FAILING(n < threshold) --failure--> INCIDENT OPEN
       ^                    |                                    |
       +------success-------+                 failure (ongoing / escalated)
       +-------------------------- success: INCIDENT RECOVERED --+

Every evaluation returns an explicit :class:`Transition`, so callers (and
tests) never have to infer what happened from side effects. CloudPulse only
records observed facts — status codes, error types, timings. It never
assigns a root cause or claims an incident was "fixed"; RECOVERED means the
service passed a health check again.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, get_args
from uuid import uuid4

from cloudpulse.clock import Clock, parse_utc_iso, to_utc_iso, utc_now
from cloudpulse.errors import StorageCorruptError

IncidentStatus = Literal["OPEN", "RECOVERED"]
IncidentSeverity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]

STATUSES: tuple[str, ...] = get_args(IncidentStatus)
SEVERITIES: tuple[str, ...] = get_args(IncidentSeverity)


def validate_severity(value: str) -> IncidentSeverity:
    if value not in SEVERITIES:
        raise ValueError(f"Severity must be one of {', '.join(SEVERITIES)}.")
    return value  # type: ignore[return-value]


@dataclass
class Incident:
    """Represent a service incident."""

    service_url: str
    severity: IncidentSeverity = "HIGH"
    id: str = field(default_factory=lambda: str(uuid4()))
    status: IncidentStatus = "OPEN"
    created_at: datetime = field(default_factory=utc_now)
    recovered_at: datetime | None = None
    service_name: str | None = None
    updated_at: datetime | None = None
    failure_count: int = 0
    first_failure_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None
    last_error_type: str | None = None
    last_status_code: int | None = None
    escalated_at: datetime | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_severity(self.severity)
        if self.status not in STATUSES:
            raise ValueError(f"Status must be one of {', '.join(STATUSES)}.")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC).")
        if self.service_name is None:
            self.service_name = self.service_url
        if self.updated_at is None:
            self.updated_at = self.created_at

    @property
    def is_open(self) -> bool:
        return self.status == "OPEN"

    def record_failure(self, result: dict[str, Any] | None, at: datetime) -> None:
        """Attach the details of another failed check to this incident."""
        self.failure_count += 1
        self.last_failure_at = at
        self.updated_at = at
        if self.first_failure_at is None:
            self.first_failure_at = at
        if result:
            self.last_error = result.get("error")
            self.last_error_type = result.get("error_type")
            self.last_status_code = result.get("status_code")
            if result.get("error_type"):
                counts = self.diagnostics.setdefault("error_types", {})
                key = str(result["error_type"])
                counts[key] = int(counts.get(key, 0)) + 1

    def recover(self, at: datetime | None = None) -> None:
        """Mark the incident as recovered (idempotent)."""
        if self.status == "RECOVERED":
            return
        when = at or utc_now()
        self.status = "RECOVERED"
        self.recovered_at = when
        self.updated_at = when

    def to_dict(self) -> dict[str, Any]:
        return incident_to_dict(self)


def _iso(value: datetime | None) -> str | None:
    return to_utc_iso(value) if value is not None else None


def incident_to_dict(incident: Incident) -> dict[str, Any]:
    """Convert an incident into a JSON-compatible dictionary."""
    duration = None
    if incident.recovered_at is not None:
        duration = round(
            (incident.recovered_at - incident.created_at).total_seconds(), 3
        )
    return {
        "id": incident.id,
        "service_name": incident.service_name,
        "service_url": incident.service_url,
        "severity": incident.severity,
        "status": incident.status,
        "created_at": _iso(incident.created_at),
        "updated_at": _iso(incident.updated_at),
        "recovered_at": _iso(incident.recovered_at),
        "duration_seconds": duration,
        "failure_count": incident.failure_count,
        "first_failure_at": _iso(incident.first_failure_at),
        "last_failure_at": _iso(incident.last_failure_at),
        "last_error": incident.last_error,
        "last_error_type": incident.last_error_type,
        "last_status_code": incident.last_status_code,
        "escalated_at": _iso(incident.escalated_at),
        "diagnostics": dict(incident.diagnostics),
    }


def _opt_time(record: dict[str, Any], key: str) -> datetime | None:
    value = record.get(key)
    return parse_utc_iso(value) if value else None


def incident_from_dict(record: dict[str, Any]) -> Incident:
    """Rebuild an :class:`Incident`; raise ``StorageCorruptError`` on bad data."""
    try:
        status_code = record.get("last_status_code")
        return Incident(
            id=str(record["id"]),
            service_url=str(record["service_url"]),
            service_name=record.get("service_name") or record["service_url"],
            severity=record.get("severity", "HIGH"),
            status=record["status"],
            created_at=parse_utc_iso(record["created_at"]),
            updated_at=_opt_time(record, "updated_at"),
            recovered_at=_opt_time(record, "recovered_at"),
            failure_count=int(record.get("failure_count") or 0),
            first_failure_at=_opt_time(record, "first_failure_at"),
            last_failure_at=_opt_time(record, "last_failure_at"),
            last_error=record.get("last_error"),
            last_error_type=record.get("last_error_type"),
            last_status_code=int(status_code) if status_code is not None else None,
            escalated_at=_opt_time(record, "escalated_at"),
            diagnostics=dict(record.get("diagnostics") or {}),
        )
    except (KeyError, TypeError, ValueError) as exc:
        incident_id = record.get("id", "<unknown>") if isinstance(record, dict) else "?"
        raise StorageCorruptError(
            f"Incident record {incident_id!r} is invalid: {exc}"
        ) from exc


class Transition(StrEnum):
    """What a single evaluation did to the service's incident state."""

    NONE = "NONE"
    FAILURE_RECORDED = "FAILURE_RECORDED"
    INCIDENT_OPENED = "INCIDENT_OPENED"
    INCIDENT_ONGOING = "INCIDENT_ONGOING"
    INCIDENT_ESCALATED = "INCIDENT_ESCALATED"
    INCIDENT_RECOVERED = "INCIDENT_RECOVERED"


@dataclass(frozen=True)
class TransitionResult:
    transition: Transition
    incident: Incident | None = None


@dataclass
class ServiceIncidentTracker:
    """Track consecutive failures and manage at most one active incident."""

    service_url: str
    failure_threshold: int = 3
    consecutive_failures: int = 0
    active_incident: Incident | None = None
    service_name: str | None = None
    severity: IncidentSeverity = "HIGH"
    escalation_threshold: int | None = None
    clock: Clock = utc_now

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if self.escalation_threshold is not None and (
            self.escalation_threshold <= self.failure_threshold
        ):
            raise ValueError("escalation_threshold must exceed failure_threshold")
        validate_severity(self.severity)
        if self.service_name is None:
            self.service_name = self.service_url

    # -- explicit transitions ---------------------------------------------

    def evaluate(self, result: dict[str, Any]) -> TransitionResult:
        """Apply one health-check result and report the transition."""
        if result.get("is_healthy"):
            return self._on_success()
        return self._on_failure(result)

    def _on_failure(self, result: dict[str, Any] | None) -> TransitionResult:
        now = self.clock()
        self.consecutive_failures += 1
        incident = self.active_incident

        if incident is not None:
            incident.record_failure(result, now)
            if (
                self.escalation_threshold is not None
                and self.consecutive_failures >= self.escalation_threshold
                and incident.severity != "CRITICAL"
            ):
                incident.diagnostics["escalated_from"] = incident.severity
                incident.severity = "CRITICAL"
                incident.escalated_at = now
                return TransitionResult(Transition.INCIDENT_ESCALATED, incident)
            return TransitionResult(Transition.INCIDENT_ONGOING, incident)

        if self.consecutive_failures < self.failure_threshold:
            return TransitionResult(Transition.FAILURE_RECORDED)

        incident = Incident(
            service_url=self.service_url,
            service_name=self.service_name,
            severity=self.severity,
            created_at=now,
            diagnostics={"failure_threshold": self.failure_threshold},
        )
        incident.record_failure(result, now)
        # Count the failures that led up to the threshold as well.
        incident.failure_count = self.consecutive_failures
        self.active_incident = incident
        return TransitionResult(Transition.INCIDENT_OPENED, incident)

    def _on_success(self) -> TransitionResult:
        self.consecutive_failures = 0
        incident = self.active_incident
        if incident is None:
            return TransitionResult(Transition.NONE)
        incident.recover(self.clock())
        self.active_incident = None
        return TransitionResult(Transition.INCIDENT_RECOVERED, incident)

    # -- backwards-compatible helpers -------------------------------------

    def record_failure(self, result: dict[str, Any] | None = None) -> Incident | None:
        """Record a failure; return the active incident, if any."""
        return self._on_failure(result).incident

    def record_success(self) -> Incident | None:
        """Reset the failure count; return the incident that recovered, if any."""
        outcome = self._on_success()
        return outcome.incident

    def restore_active_incident(self, incident: Incident) -> None:
        """Restore an existing open incident after a restart."""
        if incident.status != "OPEN":
            raise ValueError("Only open incidents can be restored.")
        if self.active_incident is not None:
            raise ValueError("An active incident already exists.")
        if incident.service_name != self.service_name:
            raise ValueError("Incident URL does not match the tracked service.")
        self.active_incident = incident

    def restore_failure_count(self, count: int) -> None:
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("Failure count must be a non-negative integer.")
        self.consecutive_failures = count
