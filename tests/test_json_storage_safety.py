"""Corruption, atomicity and legacy-format handling for JSON storage."""

import json

import pytest

from cloudpulse.errors import StorageCorruptError
from cloudpulse.incident_storage import JsonIncidentStorage
from cloudpulse.json_storage import JsonHealthCheckStorage


def test_corrupt_history_file_raises_and_is_not_overwritten(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("[{broken", encoding="utf-8")
    storage = JsonHealthCheckStorage(path)

    with pytest.raises(StorageCorruptError, match="not valid JSON"):
        storage.add({"status_code": 200})

    assert path.read_text(encoding="utf-8") == "[{broken"


def test_wrong_shape_is_reported(tmp_path):
    path = tmp_path / "incidents.json"
    path.write_text('{"not": "a list"}', encoding="utf-8")

    with pytest.raises(StorageCorruptError, match="JSON list"):
        JsonIncidentStorage(path).get_all()


def test_corrupt_state_file_raises(tmp_path):
    storage = JsonIncidentStorage(tmp_path / "incidents.json")
    storage.state_path.write_text("not json", encoding="utf-8")

    with pytest.raises(StorageCorruptError):
        storage.get_failure_count("https://example.com")


def test_state_file_with_wrong_shape(tmp_path):
    storage = JsonIncidentStorage(tmp_path / "incidents.json")
    storage.state_path.write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(StorageCorruptError, match="JSON object"):
        storage.get_state("api")


@pytest.mark.parametrize("bad", ["-1", '"two"', '{"consecutive_failures": -3}', "true"])
def test_invalid_stored_counts_are_rejected(tmp_path, bad):
    storage = JsonIncidentStorage(tmp_path / "incidents.json")
    storage.state_path.write_text(f'{{"api": {bad}}}', encoding="utf-8")

    with pytest.raises(StorageCorruptError):
        storage.get_failure_count("api")


def test_legacy_integer_state_format_is_read_and_upgraded(tmp_path):
    storage = JsonIncidentStorage(tmp_path / "incidents.json")
    storage.state_path.write_text('{"https://example.com": 2}', encoding="utf-8")

    assert storage.get_failure_count("https://example.com") == 2
    storage.save_failure_count("https://example.com", 3)

    data = json.loads(storage.state_path.read_text(encoding="utf-8"))
    assert data["https://example.com"]["consecutive_failures"] == 3
    assert data["https://example.com"]["version"] == 1


def test_empty_file_is_treated_as_empty(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("", encoding="utf-8")

    assert JsonHealthCheckStorage(path).count() == 0


def test_writes_leave_no_temporary_files(tmp_path):
    storage = JsonHealthCheckStorage(tmp_path / "history.json")
    for i in range(5):
        storage.add({"id": str(i)})

    assert sorted(p.name for p in tmp_path.iterdir()) == ["history.json"]


def test_history_is_bounded(tmp_path):
    storage = JsonHealthCheckStorage(tmp_path / "history.json", max_records=3)
    for i in range(5):
        storage.add({"id": str(i)})

    assert [r["id"] for r in storage.get_all()] == ["2", "3", "4"]


def test_storage_creates_nested_directories(tmp_path):
    storage = JsonIncidentStorage(tmp_path / "a" / "b" / "incidents.json")

    assert storage.count() == 0
