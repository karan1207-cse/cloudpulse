
"""JSON storage for CloudPulse incidents."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from cloudpulse.incident_manager import Incident


class JsonIncidentStorage:
    """Persist incident records in a local JSON file."""

    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

        if not self.file_path.exists():
            self.file_path.write_text("[]", encoding="utf-8")

    def add(self, incident: dict[str, Any]) -> None:
        """Append an incident record."""
        records = self.get_all()
        records.append(incident.copy())

        self.file_path.write_text(
            json.dumps(records, indent=2),
            encoding="utf-8",
        )

    def get_all(self) -> list[dict[str, Any]]:
        """Load all incident records."""
        data = json.loads(
            self.file_path.read_text(encoding="utf-8")
        )

        if not isinstance(data, list):
            raise TypeError(
                "Incident storage must contain a JSON list."
            )

        return data

    def update(self, incident_id: str, updates: dict[str, Any]) -> bool:
        """Update an incident by ID; return whether it was found."""
        records = self.get_all()

        for record in records:
            if record.get("id") == incident_id:
                record.update(updates)
                self.file_path.write_text(
                    json.dumps(records, indent=2),
                    encoding="utf-8",
                )
                return True

        return False

    def count(self) -> int:
        """Return the number of stored incidents."""
        return len(self.get_all())
    
    def get_failure_count(self, service_url: str) -> int:
        """Return consecutive failures recorded for a service."""
        state_path = self.file_path.with_name(
            f"{self.file_path.stem}_state.json"
        )

        if not state_path.exists():
            return 0

        data = json.loads(state_path.read_text(encoding="utf-8"))

        if not isinstance(data, dict):
            raise TypeError("Monitoring state must contain a JSON object.")

        count = data.get(service_url, 0)

        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("Stored failure count must be a non-negative integer.")

        return count

    def save_failure_count(self, service_url: str, count: int) -> None:
        """Persist consecutive failures for a service."""
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("Failure count must be a non-negative integer.")

        state_path = self.file_path.with_name(
            f"{self.file_path.stem}_state.json"
        )

        data = {}

        if state_path.exists():
            data = json.loads(state_path.read_text(encoding="utf-8"))

            if not isinstance(data, dict):
                raise TypeError("Monitoring state must contain a JSON object.")

        data[service_url] = count
        state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    
    def get_open_incident(self, service_url: str) -> Incident | None:
        """Return the latest open incident for a service."""
        records = self.get_all()

        for record in reversed(records):
            if (
                record.get("service_url") == service_url
                and record.get("status") == "OPEN"
            ):
                return Incident(
                    id=record["id"],
                    service_url=record["service_url"],
                    severity=record.get("severity", "HIGH"),
                    status="OPEN",
                    created_at=datetime.fromisoformat(
                        record["created_at"]
                    ),
                    recovered_at=None,
                )

        return None

