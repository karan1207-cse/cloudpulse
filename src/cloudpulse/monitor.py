"""Monitoring workflow for CloudPulse.

``MonitoringService`` coordinates one service: run a check, store it, apply
the incident state machine, persist state, then notify. All collaborators
(checker, storage, notifier, clock) are injected.

Persistence order on every check:

1. Health-check record is appended to history (a failure here is reported
   but does not stop incident detection).
2. Monitoring state is saved with an optimistic version check. If another
   worker saved first, this check's transition is discarded and state is
   reloaded — so two concurrent workers can never open two incidents.
3. The incident record is created/updated.
4. Notifications are sent last; delivery failures are captured and never
   affect stored incident state.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from cloudpulse.clock import Clock, parse_utc_iso, to_utc_iso, utc_now
from cloudpulse.errors import ConcurrencyConflictError, StorageError
from cloudpulse.health_check import (
    DEFAULT_ACCEPTED_STATUS,
    DEFAULT_TIMEOUT,
    HttpHealthChecker,
    check_health,
)
from cloudpulse.history import HealthCheckHistory
from cloudpulse.incident_manager import (
    Incident,
    ServiceIncidentTracker,
    Transition,
    incident_to_dict,
)
from cloudpulse.notifications import EventType, NotificationEvent, Notifier, deliver
from cloudpulse.state import ServiceState
from cloudpulse.storage import HealthCheckStorage, IncidentStorage

logger = logging.getLogger(__name__)


def serialize_incident(incident: Incident) -> dict[str, Any]:
    """Convert an incident into a JSON-compatible dictionary."""
    return incident_to_dict(incident)


_EVENTS = {
    Transition.INCIDENT_OPENED: EventType.INCIDENT_OPENED,
    Transition.INCIDENT_ESCALATED: EventType.INCIDENT_ESCALATED,
    Transition.INCIDENT_RECOVERED: EventType.INCIDENT_RECOVERED,
}


@dataclass
class CheckOutcome:
    """Everything that happened during one ``MonitoringService.run()``."""

    service: str
    result: dict[str, Any] | None = None
    transition: Transition = Transition.NONE
    incident: dict[str, Any] | None = None
    skipped: bool = False
    reason: str | None = None
    notification_error: str | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "skipped": self.skipped,
            "reason": self.reason,
            "transition": self.transition.value,
            "is_healthy": self.result.get("is_healthy") if self.result else None,
            "status_code": self.result.get("status_code") if self.result else None,
            "response_time_ms": (
                self.result.get("response_time_ms") if self.result else None
            ),
            "error_type": self.result.get("error_type") if self.result else None,
            "incident_id": self.incident.get("id") if self.incident else None,
            "notification_error": self.notification_error,
            "errors": list(self.errors),
        }


@dataclass
class MonitoringService:
    """Coordinate checks, storage, incidents, and notifications for one service."""

    service_url: str
    failure_threshold: int = 3
    history: HealthCheckStorage = field(default_factory=HealthCheckHistory)
    incident_storage: IncidentStorage | None = None
    tracker: ServiceIncidentTracker = field(init=False)
    notifications: list[str] = field(default_factory=list)
    service_name: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    severity: str = "HIGH"
    accepted_status: tuple[int, int] = DEFAULT_ACCEPTED_STATUS
    escalation_threshold: int | None = None
    checker: HttpHealthChecker | None = None
    notifier: Notifier | None = None
    clock: Clock = utc_now
    min_check_interval_seconds: float = 0.0
    state: ServiceState = field(init=False)
    restore_warnings: list[str] = field(init=False, default_factory=list)
    _stale_open_ids: list[str] = field(init=False, default_factory=list, repr=False)

    def __post_init__(self) -> None:
        """Initialize the tracker and restore persisted monitoring state."""
        if self.service_name is None:
            self.service_name = self.service_url
        self._restore()

    @property
    def name(self) -> str:
        return str(self.service_name)

    # -- restore ------------------------------------------------------------

    def _new_tracker(self) -> ServiceIncidentTracker:
        return ServiceIncidentTracker(
            service_url=self.service_url,
            service_name=self.name,
            failure_threshold=self.failure_threshold,
            severity=self.severity,  # type: ignore[arg-type]
            escalation_threshold=self.escalation_threshold,
            clock=self.clock,
        )

    def _restore(self) -> None:
        """(Re)load state from storage, repairing inconsistencies safely."""
        self.tracker = self._new_tracker()
        self.restore_warnings = []
        self._stale_open_ids = []
        self.state = ServiceState(service=self.name)
        if self.incident_storage is None:
            return

        state = self.incident_storage.get_state(self.name)
        open_incidents = self.incident_storage.get_open_incidents(self.name)
        if state is None:
            state = ServiceState(service=self.name)

        if open_incidents:
            active = open_incidents[0]
            if len(open_incidents) > 1:
                self._stale_open_ids = [i.id for i in open_incidents[1:]]
                self._warn(
                    f"Found {len(open_incidents)} open incidents for {self.name}; "
                    f"keeping the newest ({active.id}). The others will be "
                    "recovered together with it."
                )
            if state.active_incident_id and state.active_incident_id != active.id:
                self._warn(
                    f"State referenced incident {state.active_incident_id} but the "
                    f"newest open incident is {active.id}; using {active.id}."
                )
            self.tracker.restore_active_incident(active)
        elif state.active_incident_id:
            self._warn(
                f"State referenced incident {state.active_incident_id}, which is "
                "not open; clearing the reference."
            )
            state = state.with_changes(active_incident_id=None)

        self.tracker.restore_failure_count(state.consecutive_failures)
        self.state = state

    def _warn(self, message: str) -> None:
        self.restore_warnings.append(message)
        logger.warning(message)

    # -- checks -------------------------------------------------------------

    def _perform_check(self) -> dict[str, Any]:
        if self.checker is not None:
            result = self.checker.check(
                self.service_url,
                service=self.name,
                timeout=self.timeout,
                accepted_status=self.accepted_status,
            )
        else:
            result = check_health(
                self.service_url,
                self.timeout,
                service=self.name,
                accepted_status=self.accepted_status,
                clock=self.clock,
            )
        result = dict(result)
        result.setdefault("service", self.name)
        result.setdefault("checked_at", to_utc_iso(self.clock()))
        result.setdefault("error_type", None)
        if "id" not in result:
            from uuid import uuid4

            result["id"] = str(uuid4())
        return result

    def _checked_at(self, result: dict[str, Any]) -> datetime:
        """Use the check's own timestamp so state and history agree."""
        try:
            return parse_utc_iso(result["checked_at"])
        except (KeyError, TypeError, ValueError):
            return self.clock()

    def _recently_checked(self, now: datetime) -> float | None:
        if self.min_check_interval_seconds <= 0 or self.state.last_checked_at is None:
            return None
        elapsed = (now - self.state.last_checked_at).total_seconds()
        if 0 <= elapsed < self.min_check_interval_seconds:
            return elapsed
        return None

    def run_check(self) -> dict[str, Any]:
        """Run a health check and persist health and incident changes."""
        outcome = self.run()
        return outcome.result or {}

    def run(self) -> CheckOutcome:
        """Run one check and return a full :class:`CheckOutcome`."""
        outcome = CheckOutcome(service=self.name)
        elapsed = self._recently_checked(self.clock())
        if elapsed is not None:
            outcome.skipped = True
            outcome.reason = (
                f"Checked {elapsed:.0f}s ago (minimum interval "
                f"{self.min_check_interval_seconds:.0f}s); treating as duplicate."
            )
            return outcome

        result = self._perform_check()
        outcome.result = result

        try:
            self.history.add(result)
        except StorageError as exc:
            outcome.errors.append(f"history: {exc}")
            logger.error("Saving health check for %s failed: %s", self.name, exc)

        transition = self.tracker.evaluate(result)
        outcome.transition = transition.transition
        incident = transition.incident

        new_state = self.state.with_changes(
            consecutive_failures=self.tracker.consecutive_failures,
            active_incident_id=(
                self.tracker.active_incident.id
                if self.tracker.active_incident
                else None
            ),
            last_checked_at=self._checked_at(result),
            last_check_id=result.get("id"),
            last_is_healthy=bool(result.get("is_healthy")),
            last_status_code=result.get("status_code"),
            last_response_time_ms=result.get("response_time_ms"),
            last_error=result.get("error"),
            last_error_type=result.get("error_type"),
        )

        if self.incident_storage is None:
            self.state = new_state
        else:
            try:
                self.state = self.incident_storage.save_state(new_state)
            except ConcurrencyConflictError as exc:
                logger.warning("State conflict for %s: %s", self.name, exc)
                self._restore()
                outcome.transition = Transition.NONE
                outcome.skipped = True
                outcome.reason = "Another worker updated this service concurrently."
                outcome.errors.append(f"state: {exc}")
                return outcome
            if incident is not None:
                self._persist_incident(transition.transition, incident, outcome)

        if incident is not None:
            outcome.incident = incident_to_dict(incident)

        event_type = _EVENTS.get(transition.transition)
        if event_type is not None and incident is not None:
            event = NotificationEvent(event_type, incident)
            self.notifications.append(event.message)
            outcome.notification_error = deliver(self.notifier, event)

        return outcome

    def _persist_incident(
        self, transition: Transition, incident: Incident, outcome: CheckOutcome
    ) -> None:
        storage = self.incident_storage
        assert storage is not None
        record = incident_to_dict(incident)
        try:
            if transition is Transition.INCIDENT_OPENED:
                storage.add(record)
            elif not storage.update(incident.id, record):
                # The record is missing (e.g. an earlier write failed): recreate it.
                storage.add(record)
            if transition is Transition.INCIDENT_RECOVERED and self._stale_open_ids:
                for stale_id in self._stale_open_ids:
                    storage.update(
                        stale_id,
                        {
                            "status": "RECOVERED",
                            "recovered_at": record["recovered_at"],
                            "updated_at": record["updated_at"],
                        },
                    )
                self._stale_open_ids = []
        except StorageError as exc:
            outcome.errors.append(f"incident: {exc}")
            logger.error("Saving incident %s failed: %s", incident.id, exc)
