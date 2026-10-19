"""Local incident + monitoring-state storage (in-memory and JSON files)."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cloudpulse.clock import parse_utc_iso, to_utc_iso
from cloudpulse.errors import ConcurrencyConflictError, StorageCorruptError
from cloudpulse.incident_manager import Incident, incident_from_dict
from cloudpulse.jsonfile import ensure_file, file_mutex, read_json, write_json
from cloudpulse.state import ServiceState


def _service_of(record: dict[str, Any]) -> str | None:
    return record.get("service_name") or record.get("service_url")


def _validate_count(count: Any) -> int:
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError("Failure count must be a non-negative integer.")
    return count


def _validate_limit(limit: int) -> int:
    if not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer.")
    return limit


LEASE_PREFIX = "__lease__:"
SITE_PREFIX = "__site__:"
_LEASE_LOCK = threading.Lock()


class _LocalIncidentStore:
    """Shared logic for local stores; subclasses provide load/save."""

    @contextmanager
    def _state_mutex(self) -> Iterator[None]:
        with _LEASE_LOCK:
            yield

    def _load_incidents(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _save_incidents(self, records: list[dict[str, Any]]) -> None:
        raise NotImplementedError

    def _load_states(self) -> dict[str, Any]:
        raise NotImplementedError

    def _save_states(self, states: dict[str, Any]) -> None:
        raise NotImplementedError

    # -- incidents ----------------------------------------------------------

    def add(self, incident: dict[str, Any]) -> None:
        """Append an incident record (ids must be unique)."""
        if "id" not in incident:
            raise ValueError("Incident must contain an id.")
        records = self.get_all()
        if any(record.get("id") == incident["id"] for record in records):
            raise ConcurrencyConflictError(f"Incident {incident['id']} already exists.")
        records.append(incident.copy())
        self._save_incidents(records)

    def get(self, incident_id: str) -> dict[str, Any] | None:
        for record in self.get_all():
            if record.get("id") == incident_id:
                return record
        return None

    def get_all(self) -> list[dict[str, Any]]:
        """Load all incident records."""
        return self._load_incidents()

    def list_incidents(
        self,
        *,
        status: str | None = None,
        service: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        _validate_limit(limit)
        records = [
            record
            for record in self.get_all()
            if (status is None or record.get("status") == status)
            and (service is None or _service_of(record) == service)
        ]
        records.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return records[:limit]

    def update(self, incident_id: str, updates: dict[str, Any]) -> bool:
        """Update an incident by ID; return whether it was found."""
        records = self.get_all()
        for record in records:
            if record.get("id") == incident_id:
                record.update(updates)
                record["id"] = incident_id
                self._save_incidents(records)
                return True
        return False

    def count(self) -> int:
        """Return the number of stored incidents."""
        return len(self.get_all())

    def get_open_incidents(self, service: str) -> list[Incident]:
        return [
            incident_from_dict(record)
            for record in self.list_incidents(
                status="OPEN", service=service, limit=10_000
            )
        ]

    def get_open_incident(self, service_url: str) -> Incident | None:
        """Return the latest open incident for a service."""
        incidents = self.get_open_incidents(service_url)
        return incidents[0] if incidents else None

    # -- monitoring state ---------------------------------------------------

    def get_state(self, service: str) -> ServiceState | None:
        raw = self._load_states().get(service)
        if raw is None:
            return None
        if isinstance(raw, int) and not isinstance(raw, bool):
            # Legacy format: {"service": <failure count>}
            if raw < 0:
                raise StorageCorruptError(
                    "Stored failure count must be a non-negative integer."
                )
            return ServiceState(service=service, consecutive_failures=raw)
        if not isinstance(raw, dict):
            raise StorageCorruptError(f"Monitoring state for {service!r} is invalid.")
        return ServiceState.from_dict({**raw, "service": service})

    def save_state(self, state: ServiceState) -> ServiceState:
        states = self._load_states()
        current = states.get(state.service)
        current_version = 0
        if isinstance(current, dict):
            current_version = int(current.get("version", 0))
        if current_version != state.version:
            raise ConcurrencyConflictError(
                f"State for {state.service!r} changed (stored version "
                f"{current_version}, expected {state.version})."
            )
        saved = state.with_changes(version=state.version + 1)
        states[state.service] = saved.to_dict()
        self._save_states(states)
        return saved

    def get_failure_count(self, service_url: str) -> int:
        """Return consecutive failures recorded for a service."""
        state = self.get_state(service_url)
        return state.consecutive_failures if state else 0

    def save_failure_count(self, service_url: str, count: int) -> None:
        """Persist consecutive failures for a service."""
        _validate_count(count)
        state = self.get_state(service_url) or ServiceState(service=service_url)
        self.save_state(state.with_changes(consecutive_failures=count))

    # -- leases -----------------------------------------------------------

    def acquire_lease(
        self, name: str, owner: str, ttl_seconds: float, now: datetime
    ) -> bool:
        key = LEASE_PREFIX + name
        with self._state_mutex():
            states = self._load_states()
            current = states.get(key)
            if isinstance(current, dict) and current.get("owner") != owner:
                try:
                    expires = parse_utc_iso(current["expires_at"])
                except (KeyError, ValueError):
                    expires = now  # unreadable lease: treat as expired
                if expires > now:
                    return False
            states[key] = {
                "owner": owner,
                "expires_at": to_utc_iso(now + timedelta(seconds=ttl_seconds)),
            }
            self._save_states(states)
            return True

    def release_lease(self, name: str, owner: str) -> None:
        key = LEASE_PREFIX + name
        with self._state_mutex():
            states = self._load_states()
            current = states.get(key)
            if isinstance(current, dict) and current.get("owner") == owner:
                del states[key]
                self._save_states(states)

    # -- site profiles (full-site checks) --------------------------------------

    def get_site_profile(self, service: str) -> dict[str, Any] | None:
        value = self._load_states().get(SITE_PREFIX + service)
        return dict(value) if isinstance(value, dict) else None

    def save_site_profile(self, service: str, profile: dict[str, Any]) -> None:
        with self._state_mutex():
            states = self._load_states()
            states[SITE_PREFIX + service] = dict(profile)
            self._save_states(states)


class InMemoryIncidentStorage(_LocalIncidentStore):
    """Process-local incident storage (tests, demos, ``--backend memory``)."""

    def __init__(self) -> None:
        self._incidents: list[dict[str, Any]] = []
        self._states: dict[str, Any] = {}

    def _load_incidents(self) -> list[dict[str, Any]]:
        return [dict(record) for record in self._incidents]

    def _save_incidents(self, records: list[dict[str, Any]]) -> None:
        self._incidents = [dict(record) for record in records]

    def _load_states(self) -> dict[str, Any]:
        return dict(self._states)

    def _save_states(self, states: dict[str, Any]) -> None:
        self._states = dict(states)


class JsonIncidentStorage(_LocalIncidentStore):
    """Persist incident records in a local JSON file.

    Monitoring state lives next to it in ``<stem>_state.json``. Writes are
    atomic. Corrupt files raise :class:`StorageCorruptError` and are never
    overwritten. Designed for a single local process.
    """

    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)
        self.state_path = self.file_path.with_name(f"{self.file_path.stem}_state.json")
        ensure_file(self.file_path, "[]")

    @contextmanager
    def _state_mutex(self) -> Iterator[None]:
        # Leases must be exclusive across processes (CLI loop vs. `serve`).
        with _LEASE_LOCK, file_mutex(self.state_path):
            yield

    def _load_incidents(self) -> list[dict[str, Any]]:
        return read_json(self.file_path, list, [])

    def _save_incidents(self, records: list[dict[str, Any]]) -> None:
        write_json(self.file_path, records)

    def _load_states(self) -> dict[str, Any]:
        return read_json(self.state_path, dict, {})

    def _save_states(self, states: dict[str, Any]) -> None:
        write_json(self.state_path, states)
