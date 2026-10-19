"""Injectable clocks so that every timestamp is UTC and testable."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

Clock = Callable[[], datetime]
"""A zero-argument callable returning an aware UTC ``datetime``."""


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


def to_utc_iso(value: datetime) -> str:
    """Serialize an aware datetime as an ISO-8601 UTC string.

    Naive datetimes are rejected rather than silently assumed to be UTC.
    """
    if value.tzinfo is None:
        raise ValueError("Naive datetimes are not allowed; use UTC.")
    return value.astimezone(UTC).isoformat()


def parse_utc_iso(value: str) -> datetime:
    """Parse an ISO-8601 string into an aware UTC datetime.

    Strings without an offset are treated as invalid to avoid ambiguity.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("Timestamp must be a non-empty ISO-8601 string.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp has no UTC offset: {value!r}")
    return parsed.astimezone(UTC)


class FixedClock:
    """A manually advanced clock for deterministic tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 1, 1, tzinfo=UTC)
        if self.current.tzinfo is None:
            raise ValueError("FixedClock requires an aware datetime.")

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float = 0.0, **kwargs: float) -> datetime:
        """Move the clock forward and return the new time."""
        self.current = self.current + timedelta(seconds=seconds, **kwargs)
        return self.current
