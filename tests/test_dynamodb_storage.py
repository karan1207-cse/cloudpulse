"""Tests for the DynamoDB health-check storage adapter."""

from unittest.mock import MagicMock

import pytest

from cloudpulse.dynamodb_storage import DynamoDBHealthCheckStorage


def test_storage_rejects_empty_table_name():
    with pytest.raises(ValueError, match="table_name cannot be empty"):
        DynamoDBHealthCheckStorage(" ")


def test_storage_adds_health_check_record():
    table = MagicMock()
    storage = DynamoDBHealthCheckStorage(
        table_name="cloudpulse-health-checks",
        table=table,
    )

    result = {
        "url": "https://example.com",
        "is_healthy": True,
        "status_code": 200,
    }

    storage.add(result)

    table.put_item.assert_called_once()
    saved_item = table.put_item.call_args.kwargs["Item"]

    assert saved_item["url"] == "https://example.com"
    assert saved_item["is_healthy"] is True
    assert saved_item["id"]


def test_storage_reads_paginated_records():
    table = MagicMock()
    table.scan.side_effect = [
        {
            "Items": [{"id": "1"}],
            "LastEvaluatedKey": {"id": "1"},
        },
        {"Items": [{"id": "2"}]},
    ]

    storage = DynamoDBHealthCheckStorage(
        table_name="cloudpulse-health-checks",
        table=table,
    )

    assert storage.get_all() == [{"id": "1"}, {"id": "2"}]
    assert table.scan.call_count == 2


def test_storage_counts_records():
    table = MagicMock()
    table.scan.return_value = {
        "Items": [{"id": "1"}, {"id": "2"}],
    }

    storage = DynamoDBHealthCheckStorage(
        table_name="cloudpulse-health-checks",
        table=table,
    )

    assert storage.count() == 2
