
"""Tests for incident persistence in the monitoring workflow."""

from unittest.mock import patch

from cloudpulse.incident_storage import JsonIncidentStorage
from cloudpulse.monitor import MonitoringService


def test_monitor_persists_opened_and_recovered_incident(tmp_path):
    storage = JsonIncidentStorage(tmp_path / "incidents.json")
    service = MonitoringService(
        service_url="https://example.com",
        failure_threshold=2,
        incident_storage=storage,
    )

    failed_result = {
        "url": "https://example.com",
        "is_healthy": False,
        "status_code": 500,
        "response_time_ms": 10.0,
        "error": "HTTP error: 500",
    }
    healthy_result = {
        "url": "https://example.com",
        "is_healthy": True,
        "status_code": 200,
        "response_time_ms": 10.0,
        "error": None,
    }

    with patch(
        "cloudpulse.monitor.check_health",
        side_effect=[failed_result, failed_result, healthy_result],
    ):
        service.run_check()
        service.run_check()

        records = storage.get_all()
        assert len(records) == 1
        assert records[0]["status"] == "OPEN"

        service.run_check()

    records = storage.get_all()
    assert len(records) == 1
    assert records[0]["status"] == "RECOVERED"
    assert records[0]["recovered_at"] is not None
