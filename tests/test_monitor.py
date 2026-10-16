
from unittest.mock import patch

from cloudpulse.monitor import MonitoringService


def test_monitor_records_successful_check():
    service = MonitoringService("https://example.com")

    with patch(
        "cloudpulse.monitor.check_health",
        return_value={
            "url": "https://example.com",
            "is_healthy": True,
            "status_code": 200,
            "response_time_ms": 10.0,
            "error": None,
        },
    ):
        result = service.run_check()

    assert result["is_healthy"] is True
    assert service.history.count() == 1
    assert service.notifications == []


def test_monitor_opens_incident_after_threshold():
    service = MonitoringService(
        "https://example.com",
        failure_threshold=2,
    )
    failure = {
        "url": "https://example.com",
        "is_healthy": False,
        "status_code": 503,
        "response_time_ms": 20.0,
        "error": "HTTP error: 503",
    }

    with patch("cloudpulse.monitor.check_health", return_value=failure):
        service.run_check()
        service.run_check()

    assert service.tracker.active_incident is not None
    assert len(service.notifications) == 1
    assert "INCIDENT OPENED" in service.notifications[0]


def test_monitor_notifies_recovery():
    service = MonitoringService(
        "https://example.com",
        failure_threshold=1,
    )
    failure = {
        "url": "https://example.com",
        "is_healthy": False,
        "status_code": 503,
        "response_time_ms": 20.0,
        "error": "HTTP error: 503",
    }
    success = {
        "url": "https://example.com",
        "is_healthy": True,
        "status_code": 200,
        "response_time_ms": 10.0,
        "error": None,
    }

    with patch("cloudpulse.monitor.check_health", side_effect=[failure, success]):
        service.run_check()
        service.run_check()

    assert service.tracker.active_incident is None
    assert len(service.notifications) == 2
    assert "INCIDENT RECOVERED" in service.notifications[1]
