"""Check every configured service once, isolating per-service failures."""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from cloudpulse.clock import to_utc_iso
from cloudpulse.config import AppConfig, ServiceConfig
from cloudpulse.factory import Dependencies, build_monitor
from cloudpulse.incident_manager import Transition
from cloudpulse.monitor import CheckOutcome

logger = logging.getLogger(__name__)

DEFAULT_RESERVE_MS = 3000


@dataclass
class RunSummary:
    started_at: str
    finished_at: str | None = None
    outcomes: list[CheckOutcome] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)
    skipped_for_time: list[str] = field(default_factory=list)

    def _count(self, transition: Transition) -> int:
        return sum(1 for o in self.outcomes if o.transition is transition)

    @property
    def checked(self) -> list[CheckOutcome]:
        return [o for o in self.outcomes if not o.skipped and o.result is not None]

    @property
    def unhealthy(self) -> int:
        return sum(1 for o in self.checked if not (o.result or {}).get("is_healthy"))

    @property
    def errors(self) -> int:
        return len(self.failures) + sum(1 for o in self.outcomes if o.errors)

    def metrics(self) -> dict[str, int]:
        return {
            "ChecksPerformed": len(self.checked),
            "ChecksFailed": self.unhealthy,
            "IncidentsOpened": self._count(Transition.INCIDENT_OPENED),
            "IncidentsEscalated": self._count(Transition.INCIDENT_ESCALATED),
            "IncidentsRecovered": self._count(Transition.INCIDENT_RECOVERED),
            "NotificationFailures": sum(
                1 for o in self.outcomes if o.notification_error
            ),
            "ServiceErrors": self.errors,
            "ServicesSkipped": len(self.skipped_for_time)
            + sum(1 for o in self.outcomes if o.skipped),
        }

    def to_dict(self) -> dict[str, Any]:
        checked = self.checked
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "checked": len(checked),
            "healthy": len(checked) - self.unhealthy,
            "unhealthy": self.unhealthy,
            "metrics": self.metrics(),
            "services": [o.to_dict() for o in self.outcomes],
            "failures": list(self.failures),
            "skipped_for_time": list(self.skipped_for_time),
        }


def run_services(
    services: Iterable[ServiceConfig],
    deps: Dependencies,
    config: AppConfig,
    *,
    remaining_time_ms: Callable[[], int] | None = None,
    reserve_ms: int = DEFAULT_RESERVE_MS,
    force_discover: bool = False,
) -> RunSummary:
    """Run one check per service, sequentially.

    A failure for one service (storage outage, corrupt state, bug) is logged
    and recorded; the remaining services are still checked. When a time
    budget is supplied (Lambda), services that could not finish before the
    deadline are skipped and reported instead of being cut off mid-write.
    """
    summary = RunSummary(started_at=to_utc_iso(deps.clock()))
    for service in services:
        if remaining_time_ms is not None:
            needed = int(service.max_check_seconds * 1000) + reserve_ms
            if remaining_time_ms() < needed:
                summary.skipped_for_time.append(service.name)
                continue
        try:
            outcome = build_monitor(
                service, deps, config, force_discover=force_discover
            ).run()
        except Exception as exc:
            logger.exception("Checking service %s failed", service.name)
            summary.failures.append(
                {"service": service.name, "error": f"{type(exc).__name__}: {exc}"[:300]}
            )
            continue
        summary.outcomes.append(outcome)
    summary.finished_at = to_utc_iso(deps.clock())
    return summary
