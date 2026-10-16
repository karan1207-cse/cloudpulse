
from datetime import UTC

from cloudpulse.incident_manager import Incident


def test_incident_starts_open():
    incident = Incident(service_url="https://example.com")

    assert incident.status == "OPEN"
    assert incident.severity == "HIGH"
    assert incident.service_url == "https://example.com"
    assert incident.created_at.tzinfo == UTC
    assert incident.recovered_at is None


def test_incident_recovers():
    incident = Incident(service_url="https://example.com")

    incident.recover()

    assert incident.status == "RECOVERED"
    assert incident.recovered_at is not None


def test_recovering_twice_preserves_original_recovery_time():
    incident = Incident(service_url="https://example.com")

    incident.recover()
    original_recovery_time = incident.recovered_at

    incident.recover()

    assert incident.recovered_at == original_recovery_time
