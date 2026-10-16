
"""Storage interface for CloudPulse."""

from typing import Any, Protocol


class HealthCheckStorage(Protocol):
    """Define the operations required by a health-check store."""

    def add(self, result: dict[str, Any]) -> None:
        """Store a health-check result."""
        ...

    def get_all(self) -> list[dict[str, Any]]:
        """Return stored health-check results."""
        ...

    def count(self) -> int:
        """Return the number of stored results."""
        ...
