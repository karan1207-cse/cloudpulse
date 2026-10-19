"""End-to-end tests for the CloudPulse monitoring lifecycle."""

from unittest.mock import patch

from cloudpulse.incident_storage import JsonIncidentStorage
from cloudpulse.monitor import MonitoringService


def test_full_incident_lifecycle(tmp_path):
    storage = JsonIncidentStorage(tmp_path / "incidents.json")

    service = MonitoringService(
        service_url="https://example.com",
        failure_threshold=3,
        incident_storage=storage,
    )

    failure = {
        "url": "https://example.com",
        "is_healthy": False,
        "status_code": 500,
        "response_time_ms": 20.0,
        "error": "HTTP error: 500",
    }
    success = {
        "url": "https://example.com",
        "is_healthy": True,
        "status_code": 200,
        "response_time_ms": 15.0,
        "error": None,
    }

    with patch(
        "cloudpulse.monitor.check_health",
        side_effect=[failure, failure, failure, success],
    ):
        service.run_check()
        service.run_check()

        assert storage.get_all() == []

        service.run_check()

        incidents = storage.get_all()
        assert len(incidents) == 1
        assert incidents[0]["status"] == "OPEN"
        incident_id = incidents[0]["id"]

        assert len(service.notifications) == 1
        assert "incident" in service.notifications[0].lower()

        service.notifications.clear()
        service.run_check()

    incidents = storage.get_all()

    assert len(incidents) == 1
    assert incidents[0]["id"] == incident_id
    assert incidents[0]["status"] == "RECOVERED"
    assert incidents[0]["recovered_at"] is not None

    assert storage.get_failure_count("https://example.com") == 0
    assert len(service.notifications) == 1
    assert "recover" in service.notifications[0].lower()
