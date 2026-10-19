"""One contract suite run against every storage adapter.

This guarantees the in-memory, JSON and DynamoDB adapters are
interchangeable: same signatures, same return types, same semantics.
"""

import pytest

from cloudpulse.dynamodb_incident_storage import DynamoDBIncidentStorage
from cloudpulse.dynamodb_storage import DynamoDBHealthCheckStorage
from cloudpulse.errors import ConcurrencyConflictError
from cloudpulse.history import HealthCheckHistory
from cloudpulse.incident_manager import Incident
from cloudpulse.incident_storage import InMemoryIncidentStorage, JsonIncidentStorage
from cloudpulse.json_storage import JsonHealthCheckStorage
from cloudpulse.state import ServiceState
from cloudpulse.storage import HealthCheckStorage, IncidentStorage
from conftest import HEALTH_TABLE, INCIDENTS_TABLE, STATE_TABLE

BACKENDS = ["memory", "json", "dynamodb"]


@pytest.fixture(params=BACKENDS)
def stores(request, tmp_path):
    if request.param == "memory":
        return HealthCheckHistory(), InMemoryIncidentStorage()
    if request.param == "json":
        return (
            JsonHealthCheckStorage(tmp_path / "health.json"),
            JsonIncidentStorage(tmp_path / "incidents.json"),
        )
    resource = request.getfixturevalue("dynamodb")
    return (
        DynamoDBHealthCheckStorage(HEALTH_TABLE, table=resource.Table(HEALTH_TABLE)),
        DynamoDBIncidentStorage(
            INCIDENTS_TABLE,
            table=resource.Table(INCIDENTS_TABLE),
            state_table=resource.Table(STATE_TABLE),
        ),
    )


def incident(
    id_,
    service="api",
    status="OPEN",
    created="2026-01-01T00:00:00+00:00",
    severity="HIGH",
):
    return {
        "id": id_,
        "service_name": service,
        "service_url": f"https://{service}.example.com",
        "severity": severity,
        "status": status,
        "created_at": created,
        "updated_at": created,
        "recovered_at": None if status == "OPEN" else created,
        "failure_count": 3,
        "last_status_code": 503,
        "diagnostics": {"failure_threshold": 3},
    }


def check(service, checked_at, healthy=True, rt=10.5):
    return {
        "id": f"{service}-{checked_at}",
        "service": service,
        "url": f"https://{service}.example.com",
        "checked_at": checked_at,
        "is_healthy": healthy,
        "status_code": 200 if healthy else 503,
        "response_time_ms": rt,
        "error": None,
        "error_type": None,
    }


def test_adapters_satisfy_protocols(stores):
    history, incidents = stores
    assert isinstance(history, HealthCheckStorage)
    assert isinstance(incidents, IncidentStorage)


def test_history_recent_is_per_service_newest_first(stores):
    history, _ = stores
    history.add(check("api", "2026-01-01T00:00:00+00:00"))
    history.add(check("web", "2026-01-01T00:00:30+00:00"))
    history.add(check("api", "2026-01-01T00:01:00+00:00", healthy=False, rt=99.25))

    recent = history.get_recent("api", 10)

    assert [r["checked_at"] for r in recent] == [
        "2026-01-01T00:01:00+00:00",
        "2026-01-01T00:00:00+00:00",
    ]
    assert recent[0]["response_time_ms"] == 99.25
    assert isinstance(recent[0]["response_time_ms"], float)
    assert recent[0]["status_code"] == 503
    assert isinstance(recent[0]["status_code"], int)
    assert recent[0]["is_healthy"] is False
    assert history.get_recent("api", 1)[0]["checked_at"].endswith("00:01:00+00:00")
    assert history.get_recent("missing", 5) == []
    assert history.count() == 3
    assert len(history.get_all()) == 3


def test_incident_add_get_update(stores):
    _, incidents = stores
    incidents.add(incident("i-1"))

    assert incidents.get("i-1")["status"] == "OPEN"
    assert incidents.get("missing") is None
    assert incidents.update(
        "i-1", {"status": "RECOVERED", "recovered_at": "2026-01-02T00:00:00+00:00"}
    )
    assert incidents.get("i-1")["status"] == "RECOVERED"
    assert incidents.get("i-1")["diagnostics"] == {"failure_threshold": 3}
    assert incidents.update("missing", {"status": "RECOVERED"}) is False
    assert incidents.count() == 1


def test_duplicate_incident_id_is_rejected(stores):
    _, incidents = stores
    incidents.add(incident("i-1"))

    with pytest.raises(ConcurrencyConflictError):
        incidents.add(incident("i-1", status="RECOVERED"))
    assert incidents.get("i-1")["status"] == "OPEN"


def test_list_incidents_filters_and_orders(stores):
    _, incidents = stores
    incidents.add(incident("a", "api", "RECOVERED", "2026-01-01T00:00:00+00:00"))
    incidents.add(incident("b", "api", "OPEN", "2026-01-03T00:00:00+00:00"))
    incidents.add(incident("c", "web", "OPEN", "2026-01-02T00:00:00+00:00"))

    assert [r["id"] for r in incidents.list_incidents()] == ["b", "c", "a"]
    assert [r["id"] for r in incidents.list_incidents(limit=2)] == ["b", "c"]
    assert [r["id"] for r in incidents.list_incidents(status="OPEN")] == ["b", "c"]
    assert [r["id"] for r in incidents.list_incidents(service="api")] == ["b", "a"]
    assert [
        r["id"] for r in incidents.list_incidents(service="api", status="RECOVERED")
    ] == ["a"]
    with pytest.raises(ValueError):
        incidents.list_incidents(limit=0)


def test_open_incident_is_restored_as_incident_object(stores):
    _, incidents = stores
    incidents.add(incident("old", created="2026-01-01T00:00:00+00:00"))
    incidents.add(incident("new", created="2026-01-05T00:00:00+00:00"))
    incidents.add(incident("other", service="web"))

    restored = incidents.get_open_incident("api")

    assert isinstance(restored, Incident)
    assert restored.id == "new"
    assert [i.id for i in incidents.get_open_incidents("api")] == ["new", "old"]
    assert incidents.get_open_incident("nobody") is None


def test_state_is_versioned(stores):
    _, incidents = stores
    assert incidents.get_state("api") is None

    saved = incidents.save_state(ServiceState(service="api", consecutive_failures=2))
    assert saved.version == 1
    assert incidents.get_state("api") == saved

    second = incidents.save_state(saved.with_changes(consecutive_failures=3))
    assert second.version == 2

    with pytest.raises(ConcurrencyConflictError):
        incidents.save_state(saved.with_changes(consecutive_failures=9))
    with pytest.raises(ConcurrencyConflictError):
        incidents.save_state(ServiceState(service="api"))
    assert incidents.get_state("api").consecutive_failures == 3


def test_failure_count_helpers(stores):
    _, incidents = stores
    assert incidents.get_failure_count("api") == 0
    incidents.save_failure_count("api", 2)
    incidents.save_failure_count("api", 4)

    assert incidents.get_failure_count("api") == 4
    with pytest.raises(ValueError):
        incidents.save_failure_count("api", -1)


def test_state_records_are_not_counted_as_incidents(stores):
    _, incidents = stores
    incidents.save_failure_count("api", 2)
    incidents.save_failure_count("web", 1)
    incidents.add(incident("i-1"))

    assert incidents.count() == 1
    assert [r["id"] for r in incidents.get_all()] == ["i-1"]
    assert len(incidents.list_incidents()) == 1
