
"""JSON file storage for CloudPulse health-check history."""

import json
from pathlib import Path
from typing import Any


class JsonHealthCheckStorage:
    """Persist health-check results in a local JSON file."""

    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

        if not self.file_path.exists():
            self.file_path.write_text("[]", encoding="utf-8")

    def add(self, result: dict[str, Any]) -> None:
        """Append a health-check result to the JSON file."""
        records = self.get_all()
        records.append(result.copy())

        self.file_path.write_text(
            json.dumps(records, indent=2),
            encoding="utf-8",
        )

    def get_all(self) -> list[dict[str, Any]]:
        """Load all health-check results from the JSON file."""
        data = json.loads(self.file_path.read_text(encoding="utf-8"))

        if not isinstance(data, list):
            raise TypeError(
                "Health-check storage must contain a JSON list."
            )

        return data

    def count(self) -> int:
        """Return the number of persisted health-check results."""
        return len(self.get_all())
