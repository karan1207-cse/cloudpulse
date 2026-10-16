
"""Incident management for CloudPulse."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

IncidentStatus = Literal["OPEN", "RECOVERED"]
IncidentSeverity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]


@dataclass
class Incident:
    """Represent a service incident."""

    service_url: str
    severity: IncidentSeverity = "HIGH"
    id: str = field(default_factory=lambda: str(uuid4()))
    status: IncidentStatus = "OPEN"
    created_at: datetime = field(
        default_factory=lambda: datetime.now(UTC)
    )
    recovered_at: datetime | None = None

    def recover(self) -> None:
        """Mark the incident as recovered."""
        if self.status == "RECOVERED":
            return

        self.status = "RECOVERED"
        self.recovered_at = datetime.now(UTC)


@dataclass
class ServiceIncidentTracker:
    """Track consecutive failures and manage one active incident."""

    service_url: str
    failure_threshold: int = 3
    consecutive_failures: int = 0
    active_incident: Incident | None = None

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")

    def record_failure(self) -> Incident | None:
        """Open an incident after the configured failure threshold."""
        self.consecutive_failures += 1

        if (
            self.consecutive_failures >= self.failure_threshold
            and self.active_incident is None
        ):
            self.active_incident = Incident(
                service_url=self.service_url
            )

        return self.active_incident

    def record_success(self) -> Incident | None:
        """Reset the failure count and recover any active incident."""
        self.consecutive_failures = 0

        if self.active_incident is None:
            return None

        self.active_incident.recover()
        recovered_incident = self.active_incident
        self.active_incident = None

        return recovered_incident
    
    def restore_active_incident(self, incident: Incident) -> None:
        """Restore an existing open incident after a restart."""
        if incident.status != "OPEN":
            raise ValueError("Only open incidents can be restored.")

        if self.active_incident is not None:
            raise ValueError("An active incident already exists.")

        if incident.service_url != self.service_url:
            raise ValueError("Incident URL does not match the tracked service.")

        self.active_incident = incident
