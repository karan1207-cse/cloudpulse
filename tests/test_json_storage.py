
"""Tests for JSON-backed health-check storage."""

import json

from cloudpulse.json_storage import JsonHealthCheckStorage


def test_storage_creates_file(tmp_path):
    storage = JsonHealthCheckStorage(tmp_path / "history.json")

    assert storage.count() == 0


def test_storage_persists_results(tmp_path):
    file_path = tmp_path / "history.json"
    first_storage = JsonHealthCheckStorage(file_path)

    result = {
        "url": "https://example.com",
        "is_healthy": True,
        "status_code": 200,
    }

    first_storage.add(result)

    # A new instance simulates reopening storage after a restart.
    second_storage = JsonHealthCheckStorage(file_path)

    assert second_storage.count() == 1
    assert second_storage.get_all()[0] == result


def test_storage_keeps_existing_records(tmp_path):
    file_path = tmp_path / "history.json"
    storage = JsonHealthCheckStorage(file_path)

    storage.add({"status_code": 200})
    storage.add({"status_code": 503})

    records = json.loads(file_path.read_text(encoding="utf-8"))

    assert len(records) == 2
    assert records[0]["status_code"] == 200
    assert records[1]["status_code"] == 503
