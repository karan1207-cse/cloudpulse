"""DynamoDB adapter behaviour against moto (no real AWS)."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from cloudpulse.aws_common import from_dynamo, to_dynamo
from cloudpulse.dynamodb_incident_storage import DynamoDBIncidentStorage
from cloudpulse.dynamodb_storage import DynamoDBHealthCheckStorage
from cloudpulse.errors import ConcurrencyConflictError, StorageError
from cloudpulse.state import ServiceState
from conftest import HEALTH_TABLE, INCIDENTS_TABLE, STATE_TABLE

pytestmark = pytest.mark.aws


def incident_store(resource):
    return DynamoDBIncidentStorage(
        INCIDENTS_TABLE,
        table=resource.Table(INCIDENTS_TABLE),
        state_table=resource.Table(STATE_TABLE),
    )


def test_history_items_get_sort_key_and_ttl(dynamodb, clock):
    store = DynamoDBHealthCheckStorage(
        HEALTH_TABLE, table=dynamodb.Table(HEALTH_TABLE), retention_days=7, clock=clock
    )
    store.add(
        {"id": "c1", "service": "api", "is_healthy": True, "response_time_ms": 1.5}
    )

    item = dynamodb.Table(HEALTH_TABLE).scan()["Items"][0]

    assert item["sk"] == "2026-03-01T12:00:00+00:00#c1"
    assert item["checked_at"] == "2026-03-01T12:00:00+00:00"
    assert int(item["expires_at"]) == int(clock().timestamp()) + 7 * 86400
    assert item["response_time_ms"] == Decimal("1.5")


def test_history_query_paginates_beyond_one_page(dynamodb):
    store = DynamoDBHealthCheckStorage(HEALTH_TABLE, table=dynamodb.Table(HEALTH_TABLE))
    for minute in range(30):
        store.add(
            {
                "id": f"c{minute}",
                "service": "api",
                "checked_at": f"2026-01-01T00:{minute:02d}:00+00:00",
                "is_healthy": True,
            }
        )

    calls = []
    original = store.table.query

    def tracking_query(**kwargs):
        kwargs["Limit"] = min(kwargs.get("Limit", 7), 7)  # force small pages
        calls.append(kwargs)
        return original(**kwargs)

    store.table = MagicMock(wraps=store.table)
    store.table.query.side_effect = tracking_query

    recent = store.get_recent("api", 20)

    assert len(recent) == 20
    assert recent[0]["id"] == "c29"
    assert len(calls) >= 3
    assert "ExclusiveStartKey" in calls[1]
    assert store.count() == 30


def test_incident_listing_uses_indexes_not_scans(dynamodb):
    store = incident_store(dynamodb)
    store.table = MagicMock(wraps=store.table)

    store.list_incidents()
    store.list_incidents(service="api", status="OPEN")
    store.get_open_incident("api")

    store.table.scan.assert_not_called()
    indexes = {c.kwargs["IndexName"] for c in store.table.query.call_args_list}
    assert indexes == {"service-created-index", "status-created-index"}


def test_incident_listing_paginates_with_filter(dynamodb):
    store = incident_store(dynamodb)
    for i in range(12):
        store.add(
            {
                "id": f"i{i:02d}",
                "service_name": "api",
                "service_url": "https://api",
                "status": "OPEN" if i % 3 == 0 else "RECOVERED",
                "severity": "HIGH",
                "created_at": f"2026-01-{i + 1:02d}T00:00:00+00:00",
            }
        )

    open_ids = [r["id"] for r in store.list_incidents(service="api", status="OPEN")]

    assert open_ids == ["i09", "i06", "i03", "i00"]


def test_update_does_not_recreate_deleted_incident(dynamodb):
    store = incident_store(dynamodb)

    assert store.update("ghost", {"status": "RECOVERED"}) is False
    assert dynamodb.Table(INCIDENTS_TABLE).scan()["Items"] == []


def test_update_cannot_change_id(dynamodb):
    store = incident_store(dynamodb)
    store.add(
        {
            "id": "i1",
            "service_name": "a",
            "service_url": "u",
            "status": "OPEN",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )

    assert store.update("i1", {"id": "other", "status": "RECOVERED"})
    assert store.get("i1")["status"] == "RECOVERED"
    assert store.get("other") is None


def test_concurrent_state_writers_conflict(dynamodb):
    first = incident_store(dynamodb)
    second = incident_store(dynamodb)
    base = first.save_state(ServiceState(service="api"))

    first.save_state(base.with_changes(consecutive_failures=1))

    with pytest.raises(ConcurrencyConflictError):
        second.save_state(base.with_changes(consecutive_failures=1))


def test_state_operations_require_state_table():
    store = DynamoDBIncidentStorage(INCIDENTS_TABLE, table=MagicMock())

    with pytest.raises(StorageError, match="CLOUDPULSE_STATE_TABLE"):
        store.get_state("api")


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Op")


@pytest.mark.parametrize(
    "exc",
    [
        client_error("ProvisionedThroughputExceededException"),
        client_error("ResourceNotFoundException"),
        EndpointConnectionError(endpoint_url="https://dynamodb.invalid"),
    ],
)
def test_aws_errors_become_storage_errors(exc):
    table = MagicMock()
    table.put_item.side_effect = exc
    table.query.side_effect = exc
    table.get_item.side_effect = exc
    table.update_item.side_effect = exc
    history = DynamoDBHealthCheckStorage(HEALTH_TABLE, table=table)
    incidents = DynamoDBIncidentStorage(INCIDENTS_TABLE, table=table, state_table=table)

    with pytest.raises(StorageError):
        history.add({"service": "api"})
    with pytest.raises(StorageError):
        history.get_recent("api")
    with pytest.raises(StorageError):
        incidents.get("i1")
    with pytest.raises(StorageError):
        incidents.list_incidents()
    with pytest.raises(StorageError):
        incidents.get_state("api")
    if isinstance(exc, ClientError):
        with pytest.raises(StorageError):
            incidents.add({"id": "i1"})
        with pytest.raises(StorageError):
            incidents.update("i1", {"status": "RECOVERED"})
        with pytest.raises(StorageError):
            incidents.save_state(ServiceState(service="api"))


def test_missing_table_is_reported_clearly(dynamodb):
    store = DynamoDBIncidentStorage(
        "does-not-exist", table=dynamodb.Table("does-not-exist")
    )

    with pytest.raises(StorageError, match="ResourceNotFoundException"):
        store.get("i1")


def test_region_is_not_hardcoded(aws_mock, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")

    store = DynamoDBHealthCheckStorage(HEALTH_TABLE)

    assert store.table.meta.client.meta.region_name == "eu-west-1"


def test_decimal_conversion_round_trip():
    value = {"a": 1.25, "b": [2.5, 3], "c": {"d": 0.1}, "e": True, "f": None}

    converted = to_dynamo(value)

    assert converted["a"] == Decimal("1.25")
    assert converted["e"] is True
    assert from_dynamo(converted) == value
    assert isinstance(from_dynamo(Decimal("3")), int)
