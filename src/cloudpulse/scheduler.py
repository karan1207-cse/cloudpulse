"""In-process checking: "check now" and the background scheduler of `serve`.

Double-run protection:

* Within one process, every run goes through :class:`MonitorRunner`, which
  serializes runs with a lock (JSON storage is single-writer).
* Across processes, the background scheduler and ``cloudpulse run --loop``
  must hold the ``scheduler`` lease in the state storage. Whoever holds it
  checks; the other waits and reports that another scheduler is active.
* Independently, the per-service minimum-interval guard and versioned state
  writes (see :mod:`cloudpulse.monitor`) mean a stray extra run (e.g. Lambda,
  a one-off ``cloudpulse run``) can never open duplicate incidents.
"""

import logging
import os
import socket
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from cloudpulse.clock import Clock, to_utc_iso, utc_now
from cloudpulse.config import AppConfig, ServiceConfig
from cloudpulse.errors import ServiceNotFoundError
from cloudpulse.factory import Dependencies
from cloudpulse.runner import RunSummary, run_services
from cloudpulse.service_registry import ServiceRegistry

logger = logging.getLogger(__name__)

SCHEDULER_LEASE = "scheduler"


def lease_owner_id(kind: str) -> str:
    """A unique, human-readable lease owner (never contains secrets)."""
    return f"{kind}:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def lease_ttl(interval_seconds: float, services: list[ServiceConfig]) -> float:
    """Long enough to cover one full round plus the gap until the next one."""
    worst_round = sum(s.max_check_seconds for s in services)
    return interval_seconds * 2 + worst_round + 30


class MonitorRunner:
    """Runs checks for registry services, one run at a time per process."""

    def __init__(
        self,
        registry: ServiceRegistry,
        deps: Dependencies,
        config: AppConfig,
        lock: threading.Lock | None = None,
    ) -> None:
        self.registry = registry
        self.deps = deps
        self.config = config
        self.lock = lock or threading.Lock()

    def _config(self, min_interval: float) -> AppConfig:
        return replace(self.config, min_check_interval_seconds=min_interval)

    def check_service(self, name: str) -> RunSummary:
        """Check one service immediately (user request: no duplicate guard)."""
        entry = self.registry.get(name)
        if entry is None:
            raise ServiceNotFoundError(f"Service {name!r} is not configured.")
        with self.lock:
            # "Check now" also refreshes full-site discovery.
            return run_services(
                [entry.service], self.deps, self._config(0.0), force_discover=True
            )

    def check_all(self, *, min_interval: float = 0.0) -> RunSummary:
        """Check every enabled service."""
        services = self.registry.enabled_services()
        with self.lock:
            return run_services(services, self.deps, self._config(min_interval))


class BackgroundScheduler:
    """Periodically checks all enabled services from inside ``cloudpulse serve``."""

    def __init__(
        self,
        runner: MonitorRunner,
        interval_seconds: float,
        *,
        clock: Clock = utc_now,
        owner: str | None = None,
    ) -> None:
        if interval_seconds < 1:
            raise ValueError("interval_seconds must be at least 1")
        self.runner = runner
        self.interval = float(interval_seconds)
        self.clock = clock
        self.owner = owner or lease_owner_id("serve")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.is_leader = False
        self.last_run_at: datetime | None = None
        self.next_run_at: datetime | None = None
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None

    @property
    def storage(self) -> Any:
        return self.runner.deps.incidents

    def tick(self) -> RunSummary | None:
        """Run one round if this process holds the scheduler lease."""
        services = self.runner.registry.enabled_services()
        now = self.clock()
        ttl = lease_ttl(self.interval, services)
        acquired = self.storage.acquire_lease(SCHEDULER_LEASE, self.owner, ttl, now)
        if not acquired:
            if self.is_leader or self.last_run_at is None:
                logger.info(
                    "Another CloudPulse scheduler is active; this server will not "
                    "run automatic checks until it stops."
                )
            self.is_leader = False
            return None
        self.is_leader = True
        # Skip services something else checked within the last half interval.
        summary = self.runner.check_all(min_interval=self.interval / 2)
        self.last_run_at = self.clock()
        self.last_error = None
        self.last_result = {
            "checked": len(summary.checked),
            "unhealthy": summary.unhealthy,
            "failures": len(summary.failures),
        }
        return summary

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                logger.exception("Background check round failed")
            self.next_run_at = self.clock() + timedelta(seconds=self.interval)
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="cloudpulse-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        try:
            self.storage.release_lease(SCHEDULER_LEASE, self.owner)
        except Exception:
            logger.exception("Releasing the scheduler lease failed")
        self.is_leader = False

    def status(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return to_utc_iso(value) if value else None

        return {
            "enabled": True,
            "interval_seconds": self.interval,
            "active_here": self.is_leader,
            "last_run_at": iso(self.last_run_at),
            "next_run_at": iso(self.next_run_at),
            "last_result": self.last_result,
            "last_error": self.last_error,
        }
