"""Tests for explicit incident transitions, escalation and serialization."""

from datetime import UTC, datetime

import pytest

from cloudpulse.errors import StorageCorruptError
from cloudpulse.incident_manager import (
    Incident,
    ServiceIncidentTracker,
    Transition,
    incident_from_dict,
    incident_to_dict,
)
from cloudpulse.state import ServiceState
from conftest import failed_result, healthy_result


def tracker(clock, **kwargs):
    return ServiceIncidentTracker(
        "https://example.com", service_name="api", clock=clock, **kwargs
    )


def test_transitions_through_full_lifecycle(clock):
    t = tracker(clock, failure_threshold=2)

    assert t.evaluate(healthy_result()).transition is Transition.NONE
    assert t.evaluate(failed_result()).transition is Transition.FAILURE_RECORDED
    opened = t.evaluate(failed_result())
    assert opened.transition is Transition.INCIDENT_OPENED
    assert t.evaluate(failed_result()).transition is Transition.INCIDENT_ONGOING
    clock.advance(minutes=5)
    recovered = t.evaluate(healthy_result())
    assert recovered.transition is Transition.INCIDENT_RECOVERED
    assert recovered.incident is opened.incident
    assert t.evaluate(healthy_result()).transition is Transition.NONE


def test_incident_records_failure_details(clock):
    t = tracker(clock, failure_threshold=2)
    t.evaluate(failed_result(error_type="TIMEOUT", error="Request timed out."))
    clock.advance(seconds=60)
    incident = t.evaluate(failed_result()).incident

    assert incident.service_name == "api"
    assert incident.failure_count == 2
    assert incident.created_at == clock()
    assert incident.last_status_code == 503
    assert incident.last_error_type == "HTTP_STATUS"
    assert incident.diagnostics["failure_threshold"] == 2
    assert incident.diagnostics["error_types"] == {"HTTP_STATUS": 1}


def test_ongoing_failures_update_incident(clock):
    t = tracker(clock, failure_threshold=1)
    incident = t.evaluate(failed_result()).incident
    clock.advance(seconds=30)
    t.evaluate(failed_result(error_type="TIMEOUT", status_code=None))

    assert incident.failure_count == 2
    assert incident.updated_at == clock()
    assert incident.last_error_type == "TIMEOUT"
    assert incident.diagnostics["error_types"] == {"HTTP_STATUS": 1, "TIMEOUT": 1}


def test_recovery_uses_injected_clock(clock):
    t = tracker(clock, failure_threshold=1)
    t.evaluate(failed_result())
    clock.advance(minutes=10)
    incident = t.evaluate(healthy_result()).incident

    assert incident.recovered_at == clock()
    assert incident_to_dict(incident)["duration_seconds"] == 600


def test_escalation_happens_once(clock):
    t = tracker(clock, failure_threshold=1, severity="MEDIUM", escalation_threshold=3)

    t.evaluate(failed_result())
    assert t.evaluate(failed_result()).transition is Transition.INCIDENT_ONGOING
    escalated = t.evaluate(failed_result())
    assert escalated.transition is Transition.INCIDENT_ESCALATED
    assert escalated.incident.severity == "CRITICAL"
    assert escalated.incident.diagnostics["escalated_from"] == "MEDIUM"
    assert t.evaluate(failed_result()).transition is Transition.INCIDENT_ONGOING


def test_configured_severity_is_used(clock):
    t = tracker(clock, failure_threshold=1, severity="LOW")

    assert t.evaluate(failed_result()).incident.severity == "LOW"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"failure_threshold": 0},
        {"failure_threshold": 3, "escalation_threshold": 3},
        {"severity": "URGENT"},
    ],
)
def test_tracker_rejects_invalid_settings(clock, kwargs):
    with pytest.raises(ValueError):
        tracker(clock, **kwargs)


def test_restore_rejects_second_active_incident(clock):
    t = tracker(clock)
    t.restore_active_incident(
        Incident(service_url="https://example.com", service_name="api")
    )

    with pytest.raises(ValueError, match="already exists"):
        t.restore_active_incident(
            Incident(service_url="https://example.com", service_name="api")
        )


@pytest.mark.parametrize("count", [-1, True, 1.5])
def test_restore_failure_count_validates(clock, count):
    with pytest.raises(ValueError):
        tracker(clock).restore_failure_count(count)


def test_incident_rejects_invalid_fields():
    with pytest.raises(ValueError):
        Incident(service_url="x", severity="URGENT")
    with pytest.raises(ValueError):
        Incident(service_url="x", status="CLOSED")
    with pytest.raises(ValueError):
        Incident(service_url="x", created_at=datetime(2026, 1, 1))


def test_incident_round_trip(clock):
    t = tracker(clock, failure_threshold=1)
    incident = t.evaluate(failed_result()).incident
    clock.advance(seconds=5)
    t.evaluate(healthy_result())

    restored = incident_from_dict(incident_to_dict(incident))

    assert restored == incident
    assert restored.created_at.tzinfo is not None


def test_incident_from_legacy_record():
    restored = incident_from_dict(
        {
            "id": "incident-001",
            "service_url": "https://example.com",
            "severity": "HIGH",
            "status": "OPEN",
            "created_at": "2026-01-01T00:00:00+00:00",
            "recovered_at": None,
        }
    )

    assert restored.service_name == "https://example.com"
    assert restored.updated_at == restored.created_at


@pytest.mark.parametrize(
    "record",
    [
        {"id": "1"},
        {"id": "1", "service_url": "x", "status": "OPEN", "created_at": "garbage"},
        {"id": "1", "service_url": "x", "status": "OPEN", "created_at": "2026-01-01"},
        {
            "id": "1",
            "service_url": "x",
            "status": "WEIRD",
            "created_at": "2026-01-01T00:00:00Z",
        },
    ],
)
def test_incident_from_corrupt_record_raises_clear_error(record):
    with pytest.raises(StorageCorruptError, match="invalid"):
        incident_from_dict(record)


def test_service_state_round_trip():
    state = ServiceState(
        service="api",
        consecutive_failures=2,
        active_incident_id="i-1",
        last_checked_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_is_healthy=False,
        last_status_code=503,
        last_response_time_ms=12.5,
        version=4,
    )

    assert ServiceState.from_dict(state.to_dict()) == state


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"service": "api", "consecutive_failures": -1},
        {"service": "api", "consecutive_failures": "two"},
        {"service": "api", "consecutive_failures": 1.5},
        {"service": "api", "last_checked_at": "yesterday"},
    ],
)
def test_service_state_rejects_corrupt_data(data):
    with pytest.raises(StorageCorruptError):
        ServiceState.from_dict(data)
