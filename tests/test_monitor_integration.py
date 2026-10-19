"""Integration tests for MonitoringService: flows, restarts, races, failures."""

from unittest.mock import MagicMock

import pytest

from cloudpulse.dynamodb_incident_storage import DynamoDBIncidentStorage
from cloudpulse.dynamodb_storage import DynamoDBHealthCheckStorage
from cloudpulse.errors import StorageCorruptError, StorageError
from cloudpulse.history import HealthCheckHistory
from cloudpulse.incident_manager import Transition
from cloudpulse.incident_storage import InMemoryIncidentStorage, JsonIncidentStorage
from cloudpulse.json_storage import JsonHealthCheckStorage
from cloudpulse.monitor import MonitoringService
from cloudpulse.notifications import EventType, RecordingNotifier
from conftest import HEALTH_TABLE, INCIDENTS_TABLE, STATE_TABLE, ScriptedChecker

pytestmark = pytest.mark.integration


def make_service(clock, checker, storage, history=None, **kwargs):
    return MonitoringService(
        service_url="https://api.example.com/health",
        service_name="api",
        failure_threshold=kwargs.pop("failure_threshold", 3),
        history=history if history is not None else HealthCheckHistory(),
        incident_storage=storage,
        checker=checker,
        clock=clock,
        **kwargs,
    )


def test_threshold_open_then_recover_with_single_notification_each(clock):
    storage = InMemoryIncidentStorage()
    notifier = RecordingNotifier()
    checker = ScriptedChecker(clock, False, False, False, False, False, True)
    service = make_service(clock, checker, storage, notifier=notifier)

    transitions = []
    for _ in range(6):
        transitions.append(service.run().transition)
        clock.advance(seconds=60)

    assert transitions == [
        Transition.FAILURE_RECORDED,
        Transition.FAILURE_RECORDED,
        Transition.INCIDENT_OPENED,
        Transition.INCIDENT_ONGOING,
        Transition.INCIDENT_ONGOING,
        Transition.INCIDENT_RECOVERED,
    ]
    assert [e.event_type for e in notifier.events] == [
        EventType.INCIDENT_OPENED,
        EventType.INCIDENT_RECOVERED,
    ]
    [record] = storage.get_all()
    assert record["status"] == "RECOVERED"
    assert record["failure_count"] == 5
    assert record["created_at"] == "2026-03-01T12:02:00+00:00"
    assert record["recovered_at"] == "2026-03-01T12:05:00+00:00"
    state = storage.get_state("api")
    assert state.consecutive_failures == 0
    assert state.active_incident_id is None
    assert state.last_is_healthy is True


def test_counter_resets_on_success_before_threshold(clock):
    storage = InMemoryIncidentStorage()
    checker = ScriptedChecker(clock, False, False, True, False, False, True)
    service = make_service(clock, checker, storage)

    for _ in range(6):
        service.run()

    assert storage.count() == 0


@pytest.mark.parametrize("backend", ["json", "dynamodb"])
def test_restart_mid_incident_does_not_duplicate(clock, tmp_path, request, backend):
    if backend == "json":
        history = JsonHealthCheckStorage(tmp_path / "h.json")

        def new_storage():
            return JsonIncidentStorage(tmp_path / "i.json")
    else:
        resource = request.getfixturevalue("dynamodb")
        history = DynamoDBHealthCheckStorage(
            HEALTH_TABLE, table=resource.Table(HEALTH_TABLE)
        )

        def new_storage():
            return DynamoDBIncidentStorage(
                INCIDENTS_TABLE,
                table=resource.Table(INCIDENTS_TABLE),
                state_table=resource.Table(STATE_TABLE),
            )

    notifier = RecordingNotifier()
    first = make_service(
        clock, ScriptedChecker(clock, False), new_storage(), history, notifier=notifier
    )
    for _ in range(3):
        first.run()
        clock.advance(seconds=60)

    # Process restarts (new Lambda container / CLI run).
    restarted = make_service(
        clock,
        ScriptedChecker(clock, False, True),
        new_storage(),
        history,
        notifier=notifier,
    )
    assert restarted.tracker.active_incident is not None
    assert restarted.tracker.consecutive_failures == 3

    assert restarted.run().transition is Transition.INCIDENT_ONGOING
    clock.advance(seconds=60)
    assert restarted.run().transition is Transition.INCIDENT_RECOVERED

    storage = new_storage()
    assert storage.count() == 1
    assert storage.get_all()[0]["status"] == "RECOVERED"
    assert [e.event_type for e in notifier.events] == [
        EventType.INCIDENT_OPENED,
        EventType.INCIDENT_RECOVERED,
    ]
    assert history.get_recent("api", 10)[0]["is_healthy"] is True


def test_restart_restores_counter_below_threshold(clock, tmp_path):
    path = tmp_path / "i.json"
    service = make_service(
        clock, ScriptedChecker(clock, False), JsonIncidentStorage(path)
    )
    service.run()
    service.run()

    restarted = make_service(
        clock, ScriptedChecker(clock, False), JsonIncidentStorage(path)
    )
    assert restarted.run().transition is Transition.INCIDENT_OPENED


def test_notification_failure_does_not_corrupt_state(clock, failing_notifier):
    storage = InMemoryIncidentStorage()
    service = make_service(
        clock,
        ScriptedChecker(clock, False, True),
        storage,
        failure_threshold=1,
        notifier=failing_notifier,
    )

    opened = service.run()
    assert opened.transition is Transition.INCIDENT_OPENED
    assert opened.notification_error == "smtp down"
    assert storage.get_all()[0]["status"] == "OPEN"
    assert storage.get_state("api").active_incident_id == opened.incident["id"]

    recovered = service.run()
    assert recovered.transition is Transition.INCIDENT_RECOVERED
    assert storage.get_all()[0]["status"] == "RECOVERED"


def test_history_failure_does_not_block_incident_detection(clock):
    history = MagicMock()
    history.add.side_effect = StorageError("disk full")
    storage = InMemoryIncidentStorage()
    service = make_service(
        clock, ScriptedChecker(clock, False), storage, history, failure_threshold=1
    )

    outcome = service.run()

    assert outcome.transition is Transition.INCIDENT_OPENED
    assert "history: disk full" in outcome.errors
    assert storage.count() == 1


def test_concurrent_workers_open_only_one_incident(clock):
    storage = InMemoryIncidentStorage()
    notifier = RecordingNotifier()
    worker_a = make_service(
        clock,
        ScriptedChecker(clock, False),
        storage,
        failure_threshold=1,
        notifier=notifier,
    )
    worker_b = make_service(
        clock,
        ScriptedChecker(clock, False),
        storage,
        failure_threshold=1,
        notifier=notifier,
    )

    a = worker_a.run()
    b = worker_b.run()  # loaded stale state before A saved

    assert a.transition is Transition.INCIDENT_OPENED
    assert b.skipped is True
    assert b.transition is Transition.NONE
    assert storage.count() == 1
    assert len(notifier.events) == 1
    # B reloaded state and now tracks A's incident.
    assert worker_b.tracker.active_incident.id == a.incident["id"]


def test_duplicate_invocation_is_skipped(clock):
    storage = InMemoryIncidentStorage()
    checker = ScriptedChecker(clock, True)
    make_service(clock, checker, storage, min_check_interval_seconds=30).run()

    clock.advance(seconds=10)
    again = make_service(clock, checker, storage, min_check_interval_seconds=30).run()
    clock.advance(seconds=30)
    later = make_service(clock, checker, storage, min_check_interval_seconds=30).run()

    assert again.skipped is True
    assert "duplicate" in again.reason
    assert later.skipped is False
    assert checker.calls == 2


def test_inconsistent_state_pointing_to_missing_incident_is_repaired(clock):
    storage = InMemoryIncidentStorage()
    from cloudpulse.state import ServiceState

    storage.save_state(
        ServiceState(service="api", consecutive_failures=5, active_incident_id="ghost")
    )

    service = make_service(clock, ScriptedChecker(clock, False), storage)

    assert service.tracker.active_incident is None
    assert any("ghost" in w for w in service.restore_warnings)
    # Counter is kept, so the next failure re-opens an incident immediately.
    assert service.run().transition is Transition.INCIDENT_OPENED
    assert storage.get_state("api").active_incident_id != "ghost"


def test_duplicate_open_incidents_are_reconciled_on_recovery(clock):
    storage = InMemoryIncidentStorage()
    for i, day in enumerate(("01", "02")):
        storage.add(
            {
                "id": f"dup-{i}",
                "service_name": "api",
                "service_url": "https://x",
                "status": "OPEN",
                "severity": "HIGH",
                "created_at": f"2026-01-{day}T00:00:00+00:00",
            }
        )

    service = make_service(clock, ScriptedChecker(clock, True), storage)

    assert service.tracker.active_incident.id == "dup-1"
    assert service.restore_warnings
    service.run()
    assert {r["status"] for r in storage.get_all()} == {"RECOVERED"}


def test_missing_incident_record_is_recreated_on_next_failure(clock):
    storage = InMemoryIncidentStorage()
    service = make_service(
        clock, ScriptedChecker(clock, False), storage, failure_threshold=1
    )
    opened = service.run()
    storage._incidents.clear()  # simulate a lost write

    service.run()

    assert storage.get(opened.incident["id"])["failure_count"] == 2


def test_incident_storage_failure_is_reported(clock):
    storage = InMemoryIncidentStorage()
    storage.add = MagicMock(side_effect=StorageError("unavailable"))
    service = make_service(
        clock, ScriptedChecker(clock, False), storage, failure_threshold=1
    )

    outcome = service.run()

    assert outcome.transition is Transition.INCIDENT_OPENED
    assert outcome.errors == ["incident: unavailable"]


def test_corrupt_storage_fails_loudly_at_startup(clock, tmp_path):
    path = tmp_path / "i.json"
    path.write_text("{oops", encoding="utf-8")

    with pytest.raises(StorageCorruptError):
        make_service(clock, ScriptedChecker(clock, True), JsonIncidentStorage(path))


def test_results_always_carry_service_and_timestamp(clock):
    history = HealthCheckHistory()
    service = MonitoringService(
        "https://example.com",
        history=history,
        clock=clock,
        checker=MagicMock(check=MagicMock(return_value={"is_healthy": True})),
    )

    service.run()

    record = history.get_all()[0]
    assert record["service"] == "https://example.com"
    assert record["checked_at"] == "2026-03-01T12:00:00+00:00"
    assert record["id"]


def test_state_timestamp_matches_check_timestamp(clock):
    # Regression: state used the time after the check finished, so the
    # dashboard showed a different "last check" time than the history.
    storage = InMemoryIncidentStorage()
    checker = MagicMock()
    checker.check.return_value = {
        "is_healthy": True,
        "checked_at": "2026-03-01T11:59:58+00:00",
    }
    make_service(clock, checker, storage).run()

    assert storage.get_state("api").last_checked_at.isoformat() == (
        "2026-03-01T11:59:58+00:00"
    )
