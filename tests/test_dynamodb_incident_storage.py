"""Tests for DynamoDB incident storage."""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from cloudpulse.dynamodb_incident_storage import DynamoDBIncidentStorage


def test_storage_rejects_empty_table_name():
    with pytest.raises(ValueError, match="table_name cannot be empty"):
        DynamoDBIncidentStorage(" ")


def test_storage_adds_incident():
    table = MagicMock()
    storage = DynamoDBIncidentStorage(
        "cloudpulse-incidents",
        table=table,
    )

    incident = {
        "id": "incident-001",
        "service_url": "https://example.com",
        "status": "OPEN",
    }

    storage.add(incident)

    # Regression: creation must be conditional so a retried or concurrent
    # write can never silently overwrite an existing incident.
    table.put_item.assert_called_once_with(
        Item=incident,
        ConditionExpression="attribute_not_exists(id)",
    )


def test_storage_rejects_incident_without_id():
    storage = DynamoDBIncidentStorage(
        "cloudpulse-incidents",
        table=MagicMock(),
    )

    with pytest.raises(ValueError, match="must contain an id"):
        storage.add({"status": "OPEN"})


def test_storage_updates_existing_incident():
    # Regression: update used get_item + put_item (read-modify-write), which
    # loses concurrent updates. It is now a single conditional UpdateItem.
    table = MagicMock()
    storage = DynamoDBIncidentStorage(
        "cloudpulse-incidents",
        table=table,
    )

    assert storage.update(
        "incident-001",
        {"status": "RECOVERED"},
    )

    table.put_item.assert_not_called()
    table.update_item.assert_called_once_with(
        Key={"id": "incident-001"},
        UpdateExpression="SET #f0 = :v0",
        ExpressionAttributeNames={"#f0": "status"},
        ExpressionAttributeValues={":v0": "RECOVERED"},
        ConditionExpression="attribute_exists(id)",
    )


def test_storage_returns_false_for_missing_incident():
    table = MagicMock()
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
        "UpdateItem",
    )
    storage = DynamoDBIncidentStorage(
        "cloudpulse-incidents",
        table=table,
    )

    assert (
        storage.update(
            "missing-id",
            {"status": "RECOVERED"},
        )
        is False
    )
