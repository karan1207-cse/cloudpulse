"""Persisted per-service monitoring state (failure counter and more)."""

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any

from cloudpulse.clock import parse_utc_iso, to_utc_iso
from cloudpulse.errors import StorageCorruptError


@dataclass(frozen=True)
class ServiceState:
    """Monitoring state that must survive restarts.

    ``version`` is an optimistic-concurrency counter: storage adapters only
    accept a save whose ``version`` matches what is stored, then increment it.
    """

    service: str
    consecutive_failures: int = 0
    active_incident_id: str | None = None
    last_checked_at: datetime | None = None
    last_check_id: str | None = None
    last_is_healthy: bool | None = None
    last_status_code: int | None = None
    last_response_time_ms: float | None = None
    last_error: str | None = None
    last_error_type: str | None = None
    version: int = 0

    def __post_init__(self) -> None:
        if not self.service:
            raise ValueError("ServiceState.service cannot be empty.")
        count = self.consecutive_failures
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("Failure count must be a non-negative integer.")
        if not isinstance(self.version, int) or self.version < 0:
            raise ValueError("version must be a non-negative integer.")

    def with_changes(self, **changes: Any) -> "ServiceState":
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["last_checked_at"] = (
            to_utc_iso(self.last_checked_at) if self.last_checked_at else None
        )
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServiceState":
        try:
            checked = data.get("last_checked_at")
            status_code = data.get("last_status_code")
            response_time = data.get("last_response_time_ms")
            healthy = data.get("last_is_healthy")
            return cls(
                service=str(data["service"]),
                consecutive_failures=_strict_int(data.get("consecutive_failures", 0)),
                active_incident_id=data.get("active_incident_id") or None,
                last_checked_at=parse_utc_iso(checked) if checked else None,
                last_check_id=data.get("last_check_id"),
                last_is_healthy=bool(healthy) if healthy is not None else None,
                last_status_code=int(status_code) if status_code is not None else None,
                last_response_time_ms=(
                    float(response_time) if response_time is not None else None
                ),
                last_error=data.get("last_error"),
                last_error_type=data.get("last_error_type"),
                version=_strict_int(data.get("version", 0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageCorruptError(f"Monitoring state is invalid: {exc}") from exc


def _strict_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("Expected an integer, got a boolean.")
    as_int = int(value)
    if as_int != value:
        raise ValueError(f"Expected an integer, got {value!r}.")
    return as_int
