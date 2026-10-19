"""Tests for restoring active incidents."""

from datetime import UTC, datetime

import pytest

from cloudpulse.incident_manager import Incident, ServiceIncidentTracker


def test_tracker_restores_open_incident():
    incident = Incident(
        id="incident-001",
        service_url="https://example.com",
        status="OPEN",
        created_at=datetime.now(UTC),
    )
    tracker = ServiceIncidentTracker("https://example.com")

    tracker.restore_active_incident(incident)

    assert tracker.active_incident is incident


def test_tracker_rejects_recovered_incident():
    incident = Incident(
        service_url="https://example.com",
        status="RECOVERED",
    )
    tracker = ServiceIncidentTracker("https://example.com")

    with pytest.raises(ValueError):
        tracker.restore_active_incident(incident)


def test_tracker_rejects_incident_for_another_service():
    incident = Incident(service_url="https://other.example.com")
    tracker = ServiceIncidentTracker("https://example.com")

    with pytest.raises(ValueError):
        tracker.restore_active_incident(incident)
