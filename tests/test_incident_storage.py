"""Tests for JSON incident storage."""

from cloudpulse.incident_storage import JsonIncidentStorage


def test_incident_storage_starts_empty(tmp_path):
    storage = JsonIncidentStorage(tmp_path / "incidents.json")

    assert storage.count() == 0
    assert storage.get_all() == []


def test_incident_storage_persists_records(tmp_path):
    file_path = tmp_path / "incidents.json"
    storage = JsonIncidentStorage(file_path)

    incident = {
        "id": "incident-001",
        "service_url": "https://example.com",
        "status": "OPEN",
    }
    storage.add(incident)

    reopened_storage = JsonIncidentStorage(file_path)

    assert reopened_storage.count() == 1
    assert reopened_storage.get_all()[0] == incident


def test_incident_storage_updates_recovered_incident(tmp_path):
    storage = JsonIncidentStorage(tmp_path / "incidents.json")
    storage.add(
        {
            "id": "incident-001",
            "status": "OPEN",
        }
    )

    updated = storage.update(
        "incident-001",
        {"status": "RECOVERED"},
    )

    assert updated is True
    assert storage.get_all()[0]["status"] == "RECOVERED"


def test_update_returns_false_for_unknown_incident(tmp_path):
    storage = JsonIncidentStorage(tmp_path / "incidents.json")

    assert storage.update("missing-id", {"status": "RECOVERED"}) is False


def test_failure_count_persists_after_reopening_storage(tmp_path):
    storage_path = tmp_path / "incidents.json"
    storage = JsonIncidentStorage(storage_path)

    storage.save_failure_count("https://example.com", 2)

    reopened_storage = JsonIncidentStorage(storage_path)

    assert reopened_storage.get_failure_count("https://example.com") == 2
    assert reopened_storage.get_failure_count("https://other.example.com") == 0


def test_failure_count_rejects_negative_values(tmp_path):
    storage = JsonIncidentStorage(tmp_path / "incidents.json")

    import pytest

    with pytest.raises(ValueError):
        storage.save_failure_count("https://example.com", -1)
