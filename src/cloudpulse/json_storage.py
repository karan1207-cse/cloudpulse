"""JSON file storage for CloudPulse health-check history."""

from pathlib import Path
from typing import Any

from cloudpulse.history import recent_for_service
from cloudpulse.jsonfile import ensure_file, read_json, write_json

DEFAULT_MAX_RECORDS = 10_000


class JsonHealthCheckStorage:
    """Persist health-check results in a local JSON file.

    Intended for local, single-process use. Writes are atomic; the oldest
    records are dropped beyond ``max_records`` so the file stays bounded.
    """

    def __init__(
        self, file_path: str | Path, max_records: int | None = DEFAULT_MAX_RECORDS
    ) -> None:
        self.file_path = Path(file_path)
        self.max_records = max_records
        ensure_file(self.file_path, "[]")

    def add(self, result: dict[str, Any]) -> None:
        """Append a health-check result to the JSON file."""
        records = self.get_all()
        records.append(result.copy())
        if self.max_records is not None and len(records) > self.max_records:
            records = records[-self.max_records :]
        write_json(self.file_path, records)

    def get_all(self) -> list[dict[str, Any]]:
        """Load all health-check results from the JSON file."""
        return read_json(self.file_path, list, [])

    def get_recent(self, service: str, limit: int = 50) -> list[dict[str, Any]]:
        return recent_for_service(self.get_all(), service, limit)

    def count(self) -> int:
        """Return the number of persisted health-check results."""
        return len(self.get_all())
