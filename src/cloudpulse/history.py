
"""In-memory health-check history for CloudPulse."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HealthCheckHistory:
    """Store health-check results for the current process."""

    records: list[dict[str, Any]] = field(default_factory=list)

    def add(self, result: dict[str, Any]) -> None:
        """Add a health-check result."""
        self.records.append(result.copy())

    def get_all(self) -> list[dict[str, Any]]:
        """Return a copy of all stored records."""
        return [record.copy() for record in self.records]

    def count(self) -> int:
        """Return the number of stored results."""
        return len(self.records)
