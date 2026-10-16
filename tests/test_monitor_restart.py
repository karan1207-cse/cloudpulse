
"""Tests for restoring monitoring state after restart."""

from cloudpulse.incident_storage import JsonIncidentStorage
from cloudpulse.monitor import MonitoringService


def test_monitor_restores_failure_count_after_restart(tmp_path):
    storage_path = tmp_path / "incidents.json"
    storage = JsonIncidentStorage(storage_path)

    storage.save_failure_count("https://example.com", 2)

    restarted_service = MonitoringService(
        service_url="https://example.com",
        failure_threshold=3,
        incident_storage=JsonIncidentStorage(storage_path),
    )

    assert restarted_service.tracker.consecutive_failures == 2
