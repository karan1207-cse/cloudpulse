"""In-memory health-check history for CloudPulse."""

from dataclasses import dataclass, field
from typing import Any


def recent_for_service(
    records: list[dict[str, Any]], service: str, limit: int
) -> list[dict[str, Any]]:
    """Return the newest ``limit`` records for ``service``, newest first."""
    if limit < 1:
        return []
    matching = [
        record
        for record in reversed(records)  # ties: newest insertion first
        if (record.get("service") or record.get("url")) == service
    ]
    matching.sort(key=lambda r: r.get("checked_at") or "", reverse=True)
    return [record.copy() for record in matching[:limit]]


@dataclass
class HealthCheckHistory:
    """Store health-check results for the current process."""

    records: list[dict[str, Any]] = field(default_factory=list)
    max_records: int | None = 10_000

    def add(self, result: dict[str, Any]) -> None:
        """Add a health-check result."""
        self.records.append(result.copy())
        if self.max_records is not None and len(self.records) > self.max_records:
            del self.records[: len(self.records) - self.max_records]

    def get_all(self) -> list[dict[str, Any]]:
        """Return a copy of all stored records."""
        return [record.copy() for record in self.records]

    def get_recent(self, service: str, limit: int = 50) -> list[dict[str, Any]]:
        return recent_for_service(self.records, service, limit)

    def count(self) -> int:
        """Return the number of stored results."""
        return len(self.records)
