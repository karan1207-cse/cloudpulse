
"""Monitoring workflow for CloudPulse."""

from dataclasses import dataclass, field
from typing import Any

from cloudpulse.health_check import check_health
from cloudpulse.history import HealthCheckHistory
from cloudpulse.incident_manager import Incident, ServiceIncidentTracker
from cloudpulse.incident_storage import JsonIncidentStorage
from cloudpulse.notifications import (
    format_incident_opened,
    format_incident_recovered,
)
from cloudpulse.storage import HealthCheckStorage


def serialize_incident(incident: Incident) -> dict[str, Any]:
    """Convert an incident into a JSON-compatible dictionary."""
    return {
        "id": incident.id,
        "service_url": incident.service_url,
        "severity": incident.severity,
        "status": incident.status,
        "created_at": incident.created_at.isoformat(),
        "recovered_at": (
            incident.recovered_at.isoformat()
            if incident.recovered_at is not None
            else None
        ),
    }


@dataclass
class MonitoringService:
    """Coordinate checks, storage, incidents, and notifications."""

    service_url: str
    failure_threshold: int = 3
    history: HealthCheckStorage = field(
        default_factory=HealthCheckHistory
    )
    incident_storage: JsonIncidentStorage | None = None
    tracker: ServiceIncidentTracker = field(init=False)
    notifications: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Initialize the tracker and restore persisted monitoring state."""
        self.tracker = ServiceIncidentTracker(
            service_url=self.service_url,
            failure_threshold=self.failure_threshold,
        )

        if self.incident_storage is None:
            return

        open_incident = self.incident_storage.get_open_incident(
            self.service_url
        )

        if open_incident is not None:
            self.tracker.restore_active_incident(open_incident)

        self.tracker.consecutive_failures = (
            self.incident_storage.get_failure_count(self.service_url)
        )

    def run_check(self) -> dict[str, Any]:
        """Run a health check and persist health and incident changes."""
        result = check_health(self.service_url)
        self.history.add(result)

        if result["is_healthy"]:
            self._handle_success()
        else:
            self._handle_failure()

        self._persist_failure_count()

        return result

    def _handle_success(self) -> None:
        """Reset failures and record recovery when an incident is active."""
        recovered = self.tracker.record_success()

        if recovered is None:
            return

        if self.incident_storage is not None:
            self.incident_storage.update(
                recovered.id,
                serialize_incident(recovered),
            )

        self.notifications.append(
            format_incident_recovered(recovered)
        )

    def _handle_failure(self) -> None:
        """Track failures and persist newly opened incidents."""
        previous_incident = self.tracker.active_incident
        incident = self.tracker.record_failure()

        if previous_incident is not None or incident is None:
            return

        if self.incident_storage is not None:
            self.incident_storage.add(serialize_incident(incident))

        self.notifications.append(format_incident_opened(incident))

    def _persist_failure_count(self) -> None:
        """Save the current consecutive-failure count."""
        if self.incident_storage is None:
            return

        self.incident_storage.save_failure_count(
            self.service_url,
            self.tracker.consecutive_failures,
        )
