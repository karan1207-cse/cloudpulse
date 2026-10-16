
"""Monitoring workflow for CloudPulse."""

from dataclasses import dataclass, field
from typing import Any

from cloudpulse.health_check import check_health
from cloudpulse.history import HealthCheckHistory
from cloudpulse.incident_manager import ServiceIncidentTracker
from cloudpulse.notifications import (
    format_incident_opened,
    format_incident_recovered,
)


@dataclass
class MonitoringService:
    """Coordinate health checks, history, incidents, and notifications."""

    service_url: str
    failure_threshold: int = 3
    history: HealthCheckHistory = field(default_factory=HealthCheckHistory)
    tracker: ServiceIncidentTracker = field(init=False)
    notifications: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.tracker = ServiceIncidentTracker(
            service_url=self.service_url,
            failure_threshold=self.failure_threshold,
        )

    def run_check(self) -> dict[str, Any]:
        """Run one check and update the monitoring state."""
        result = check_health(self.service_url)
        self.history.add(result)

        if result["is_healthy"]:
            recovered = self.tracker.record_success()

            if recovered is not None:
                self.notifications.append(
                    format_incident_recovered(recovered)
                )
        else:
            previous_incident = self.tracker.active_incident
            incident = self.tracker.record_failure()

            if previous_incident is None and incident is not None:
                self.notifications.append(
                    format_incident_opened(incident)
                )

        return result
