"""The single list of monitored services used by the CLI, API, scheduler and
Lambda.

Two sources are merged:

* **config** — services from ``CLOUDPULSE_SERVICES_FILE`` /
  ``CLOUDPULSE_SERVICES`` (backward compatible, read-only through the API;
  ``cloudpulse services import`` copies them into the store to make them
  editable);
* **dashboard** — services created through the dashboard/API/CLI and kept in
  a :class:`~cloudpulse.service_store.ServiceStore`.

If both define the same name, the config entry wins.
"""

import logging
from dataclasses import dataclass
from typing import Any

from cloudpulse.config import (
    MAX_SERVICES,
    PRIVATE_TARGET_HINT,
    AppConfig,
    ServiceConfig,
    parse_service,
)
from cloudpulse.errors import (
    ConcurrencyConflictError,
    ServiceNotFoundError,
    ServiceReadOnlyError,
    ServiceValidationError,
    UnsafeURLError,
)
from cloudpulse.service_store import ServiceStore
from cloudpulse.url_safety import BlockedDestinationError, default_port, validate_url

logger = logging.getLogger(__name__)

SOURCE_CONFIG = "config"
SOURCE_DASHBOARD = "dashboard"


@dataclass(frozen=True)
class ServiceEntry:
    service: ServiceConfig
    source: str

    @property
    def editable(self) -> bool:
        return self.source == SOURCE_DASHBOARD


class ServiceRegistry:
    def __init__(self, config: AppConfig, store: ServiceStore | None) -> None:
        self.config = config
        self.store = store

    # -- reading -------------------------------------------------------------

    def entries(self) -> list[ServiceEntry]:
        result = [ServiceEntry(s, SOURCE_CONFIG) for s in self.config.services]
        config_names = {s.name for s in self.config.services}
        if self.store is not None:
            for service in self.store.list_services():
                if service.name in config_names:
                    logger.warning(
                        "Service %r is defined in configuration and in the "
                        "service store; using the configuration entry.",
                        service.name,
                    )
                    continue
                result.append(ServiceEntry(service, SOURCE_DASHBOARD))
        return result

    def services(self) -> list[ServiceConfig]:
        return [e.service for e in self.entries()]

    def enabled_services(self) -> list[ServiceConfig]:
        return [s for s in self.services() if s.enabled]

    def get(self, name: str) -> ServiceEntry | None:
        return next((e for e in self.entries() if e.service.name == name), None)

    @property
    def can_store(self) -> bool:
        return self.store is not None

    # -- validation ----------------------------------------------------------

    def validate(self, data: Any) -> tuple[ServiceConfig, list[str]]:
        """Validate API input; return the service and non-fatal warnings."""
        if isinstance(data, dict):
            data = {
                k: (v.strip() if isinstance(v, str) and k in ("name", "url") else v)
                for k, v in data.items()
            }
        service = parse_service(
            data,
            index=None,
            default_timeout=self.config.default_timeout,
            default_threshold=self.config.default_failure_threshold,
            policy=self.config.url_policy,
        )
        return service, self._dns_warnings(service)

    def _dns_warnings(self, service: ServiceConfig) -> list[str]:
        """Resolve the host now so private destinations are rejected up front.

        The checker repeats this (and checks the connected peer) on every
        check, so this is a usability step, not the only line of defence.
        """
        if self.config.allow_private_targets:
            return []
        parsed = validate_url(service.url)
        try:
            self.config.url_policy.check_resolved(
                parsed.hostname or "", default_port(parsed)
            )
        except BlockedDestinationError as exc:
            raise ServiceValidationError("url", f"{exc} {PRIVATE_TARGET_HINT}") from exc
        except UnsafeURLError as exc:
            raise ServiceValidationError("url", str(exc)) from exc
        except (OSError, UnicodeError):
            return [
                "We could not look up this website's address right now. It was "
                "saved anyway; checks will report DNS errors until it resolves."
            ]
        return []

    def validate_component_url(self, url: str) -> str:
        """Validate a manually added page/backend URL (syntax, SSRF, DNS)."""
        try:
            self.config.url_policy.check_url(url)
        except BlockedDestinationError as exc:
            raise ServiceValidationError("url", f"{exc} {PRIVATE_TARGET_HINT}") from exc
        except UnsafeURLError as exc:
            raise ServiceValidationError("url", str(exc)) from exc
        self._dns_warnings(ServiceConfig(name="component", url=url))
        return url

    # -- writing -------------------------------------------------------------

    def _require_store(self) -> ServiceStore:
        if self.store is None:
            raise ServiceReadOnlyError(
                "Services cannot be changed here: no service store is configured "
                "(set CLOUDPULSE_SERVICES_TABLE for the aws backend)."
            )
        return self.store

    def create(self, data: Any) -> tuple[ServiceConfig, list[str]]:
        store = self._require_store()
        service, warnings = self.validate(data)
        if self.get(service.name) is not None:
            raise ServiceValidationError(
                "name", f"A service named {service.name!r} already exists."
            )
        if len(self.entries()) >= MAX_SERVICES:
            raise ServiceValidationError(
                None,
                f"You can monitor at most {MAX_SERVICES} services. "
                "Delete one before adding another.",
            )
        try:
            store.add_service(service)
        except ConcurrencyConflictError as exc:
            raise ServiceValidationError(
                "name", f"A service named {service.name!r} already exists."
            ) from exc
        return service, warnings

    def update(self, name: str, data: Any) -> tuple[ServiceConfig, list[str]]:
        store = self._require_store()
        entry = self.get(name)
        if entry is None:
            raise ServiceNotFoundError(f"Service {name!r} is not configured.")
        if not entry.editable:
            raise ServiceReadOnlyError(
                f"Service {name!r} is defined in the services file; edit it there "
                "or run `cloudpulse services import` to manage it here."
            )
        if isinstance(data, dict):
            if data.get("name", name) != name:
                raise ServiceValidationError(
                    "name",
                    "A service cannot be renamed. Add a new service and delete "
                    "this one instead.",
                )
            data = {**data, "name": name}
        service, warnings = self.validate(data)
        if not store.replace_service(service):
            raise ServiceNotFoundError(f"Service {name!r} is not configured.")
        return service, warnings

    def delete(self, name: str) -> None:
        store = self._require_store()
        entry = self.get(name)
        if entry is None:
            raise ServiceNotFoundError(f"Service {name!r} is not configured.")
        if not entry.editable:
            raise ServiceReadOnlyError(
                f"Service {name!r} is defined in the services file; remove it there."
            )
        if not store.delete_service(name):
            raise ServiceNotFoundError(f"Service {name!r} is not configured.")

    def import_config_services(self) -> list[str]:
        """Copy config-file services into the store (skipping existing names)."""
        store = self._require_store()
        imported = []
        existing = {s.name for s in store.list_services()}
        for service in self.config.services:
            if service.name in existing:
                continue
            store.add_service(service)
            imported.append(service.name)
        return imported
