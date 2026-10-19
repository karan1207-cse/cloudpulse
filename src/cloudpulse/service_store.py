"""Persistence for services added through the dashboard/API.

Three adapters share one protocol so the CLI, the local API and Lambda all
see the same list:

* :class:`InMemoryServiceStore` (tests, ``--backend memory``)
* :class:`JsonServiceStore` (``data/services.json``, local default)
* :class:`DynamoDBServiceStore` (``CLOUDPULSE_SERVICES_TABLE``; PK ``name``)

Records are re-validated when read, so a hand-edited or corrupt record raises
:class:`StorageCorruptError` instead of reaching the checker.
"""

import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from cloudpulse.aws_common import (
    ClientError,
    aws_call,
    error_code,
    from_dynamo,
    paginate,
    to_dynamo,
)
from cloudpulse.clock import Clock, to_utc_iso, utc_now
from cloudpulse.config import ServiceConfig, parse_service
from cloudpulse.errors import (
    ConcurrencyConflictError,
    ServiceValidationError,
    StorageCorruptError,
    StorageError,
)
from cloudpulse.jsonfile import ensure_file, read_json, write_json
from cloudpulse.url_safety import UrlPolicy

# Stored services were validated (including the SSRF policy) when written.
# On read only the shape is re-checked; destinations are enforced again by the
# checker at every check, so a policy change still takes effect.
_READ_POLICY = UrlPolicy(allow_private=True)
_METADATA_KEYS = ("created_at", "updated_at")


def service_from_record(record: Any) -> ServiceConfig:
    if not isinstance(record, dict):
        raise StorageCorruptError("Stored service record is not an object.")
    data = {k: v for k, v in record.items() if k not in _METADATA_KEYS}
    try:
        return parse_service(
            data,
            index=None,
            default_timeout=5.0,
            default_threshold=3,
            policy=_READ_POLICY,
        )
    except ServiceValidationError as exc:
        name = record.get("name", "<unknown>")
        raise StorageCorruptError(f"Stored service {name!r} is invalid: {exc}") from exc


@runtime_checkable
class ServiceStore(Protocol):
    def list_services(self) -> list[ServiceConfig]:
        """All stored services, sorted by name."""
        ...

    def get_service(self, name: str) -> ServiceConfig | None: ...

    def add_service(self, service: ServiceConfig) -> None:
        """Create; raise ``ConcurrencyConflictError`` if the name exists."""
        ...

    def replace_service(self, service: ServiceConfig) -> bool:
        """Replace an existing service; return False if it does not exist."""
        ...

    def delete_service(self, name: str) -> bool:
        """Delete; return False if it did not exist."""
        ...


class _DictServiceStore:
    """Shared logic for stores that load/save a ``{name: record}`` mapping."""

    def __init__(self, clock: Clock = utc_now) -> None:
        self.clock = clock
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        raise NotImplementedError

    def _save(self, records: dict[str, Any]) -> None:
        raise NotImplementedError

    def list_services(self) -> list[ServiceConfig]:
        return [service_from_record(r) for _, r in sorted(self._load().items())]

    def get_service(self, name: str) -> ServiceConfig | None:
        record = self._load().get(name)
        return service_from_record(record) if record is not None else None

    def add_service(self, service: ServiceConfig) -> None:
        with self._lock:
            records = self._load()
            if service.name in records:
                raise ConcurrencyConflictError(f"Service {service.name!r} exists.")
            now = to_utc_iso(self.clock())
            records[service.name] = {
                **service.to_storage_dict(),
                "created_at": now,
                "updated_at": now,
            }
            self._save(records)

    def replace_service(self, service: ServiceConfig) -> bool:
        with self._lock:
            records = self._load()
            existing = records.get(service.name)
            if existing is None:
                return False
            records[service.name] = {
                **service.to_storage_dict(),
                "created_at": existing.get("created_at"),
                "updated_at": to_utc_iso(self.clock()),
            }
            self._save(records)
            return True

    def delete_service(self, name: str) -> bool:
        with self._lock:
            records = self._load()
            if records.pop(name, None) is None:
                return False
            self._save(records)
            return True


class InMemoryServiceStore(_DictServiceStore):
    def __init__(self, clock: Clock = utc_now) -> None:
        super().__init__(clock)
        self._records: dict[str, Any] = {}

    def _load(self) -> dict[str, Any]:
        return {k: dict(v) for k, v in self._records.items()}

    def _save(self, records: dict[str, Any]) -> None:
        self._records = {k: dict(v) for k, v in records.items()}


class JsonServiceStore(_DictServiceStore):
    """``data/services.json`` — atomic writes, never overwritten when corrupt."""

    def __init__(self, file_path: str | Path, clock: Clock = utc_now) -> None:
        super().__init__(clock)
        self.file_path = Path(file_path)
        ensure_file(self.file_path, "{}")

    def _load(self) -> dict[str, Any]:
        return read_json(self.file_path, dict, {})

    def _save(self, records: dict[str, Any]) -> None:
        write_json(self.file_path, records)


class DynamoDBServiceStore:
    """Services table (PK ``name``). Listing scans the table, which is bounded
    by the 50-service limit (one page)."""

    def __init__(self, table: Any, clock: Clock = utc_now) -> None:
        self.table = table
        self.clock = clock

    def list_services(self) -> list[ServiceConfig]:
        items = paginate("Listing services", self.table.scan)
        return sorted((service_from_record(i) for i in items), key=lambda s: s.name)

    def get_service(self, name: str) -> ServiceConfig | None:
        response = aws_call(
            "Reading service",
            self.table.get_item,
            Key={"name": name},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return service_from_record(from_dynamo(item)) if item else None

    def _put(
        self, service: ServiceConfig, condition: str, created_at: str | None
    ) -> None:
        now = to_utc_iso(self.clock())
        item = {
            **service.to_storage_dict(),
            "created_at": created_at or now,
            "updated_at": now,
        }
        self.table.put_item(
            Item=to_dynamo(item),
            ConditionExpression=condition,
            ExpressionAttributeNames={"#n": "name"},
        )

    def add_service(self, service: ServiceConfig) -> None:
        try:
            self._put(service, "attribute_not_exists(#n)", None)
        except ClientError as exc:
            if error_code(exc) == "ConditionalCheckFailedException":
                raise ConcurrencyConflictError(
                    f"Service {service.name!r} exists."
                ) from exc
            raise StorageError(f"Saving service failed: {error_code(exc)}") from exc

    def replace_service(self, service: ServiceConfig) -> bool:
        response = aws_call(
            "Reading service",
            self.table.get_item,
            Key={"name": service.name},
            ConsistentRead=True,
        )
        existing = response.get("Item")
        if not existing:
            return False
        try:
            self._put(service, "attribute_exists(#n)", existing.get("created_at"))
        except ClientError as exc:
            if error_code(exc) == "ConditionalCheckFailedException":
                return False
            raise StorageError(f"Saving service failed: {error_code(exc)}") from exc
        return True

    def delete_service(self, name: str) -> bool:
        response = aws_call(
            "Deleting service",
            self.table.delete_item,
            Key={"name": name},
            ReturnValues="ALL_OLD",
        )
        return bool(response.get("Attributes"))
