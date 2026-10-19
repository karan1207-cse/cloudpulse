"""Storage interfaces for CloudPulse.

Every adapter (in-memory, JSON, DynamoDB) implements the same protocols with
the same signatures and return types; ``tests/test_storage_contract.py`` runs
one shared test suite against each of them.
"""

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from cloudpulse.incident_manager import Incident
from cloudpulse.state import ServiceState


@runtime_checkable
class HealthCheckStorage(Protocol):
    """Define the operations required by health-check storage."""

    def add(self, result: dict[str, Any]) -> None:
        """Store a health-check result."""
        ...

    def get_all(self) -> list[dict[str, Any]]:
        """Return stored health-check results (administrative; may be slow)."""
        ...

    def get_recent(self, service: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return the newest results for one service, newest first."""
        ...

    def count(self) -> int:
        """Return the number of stored health-check results."""
        ...


@runtime_checkable
class IncidentStorage(Protocol):
    """Define the operations required by incident + monitoring-state storage.

    Raises :class:`cloudpulse.errors.StorageError` subclasses on failure.
    """

    def add(self, incident: dict[str, Any]) -> None:
        """Store a new incident; raise ``ConcurrencyConflictError`` if its id exists."""
        ...

    def get(self, incident_id: str) -> dict[str, Any] | None:
        """Return one incident or ``None``."""
        ...

    def get_all(self) -> list[dict[str, Any]]:
        """Return all stored incidents (administrative; may be slow)."""
        ...

    def list_incidents(
        self,
        *,
        status: str | None = None,
        service: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return incidents newest first, optionally filtered."""
        ...

    def update(self, incident_id: str, updates: dict[str, Any]) -> bool:
        """Update an incident and report whether it was found."""
        ...

    def get_open_incident(self, service_url: str) -> Incident | None:
        """Return the newest active incident for a service."""
        ...

    def get_open_incidents(self, service: str) -> list[Incident]:
        """Return every OPEN incident for a service, newest first."""
        ...

    def get_state(self, service: str) -> ServiceState | None:
        """Return the persisted monitoring state for a service."""
        ...

    def save_state(self, state: ServiceState) -> ServiceState:
        """Persist ``state`` if its version matches; return it with version + 1.

        Raises ``ConcurrencyConflictError`` when another writer won the race.
        """
        ...

    def get_failure_count(self, service_url: str) -> int:
        """Return the saved consecutive-failure count."""
        ...

    def save_failure_count(self, service_url: str, count: int) -> None:
        """Persist the consecutive-failure count."""
        ...

    def count(self) -> int:
        """Return the number of stored incidents (never state records)."""
        ...

    def acquire_lease(
        self, name: str, owner: str, ttl_seconds: float, now: datetime
    ) -> bool:
        """Take or renew an exclusive, expiring lease (e.g. "scheduler").

        Returns True if ``owner`` now holds it; False if someone else does.
        """
        ...

    def release_lease(self, name: str, owner: str) -> None:
        """Release the lease if ``owner`` holds it."""
        ...

    def get_site_profile(self, service: str) -> dict[str, Any] | None:
        """Full-site-check discovery cache and user overrides for a service."""
        ...

    def save_site_profile(self, service: str, profile: dict[str, Any]) -> None:
        """Persist the site profile (last write wins)."""
        ...
