from cloudpulse.incident_manager import Incident
from cloudpulse.notifications import (
    format_incident_opened,
    format_incident_recovered,
)


def test_open_notification_contains_incident_details():
    incident = Incident(service_url="https://example.com")

    message = format_incident_opened(incident)

    assert "INCIDENT OPENED" in message
    assert "https://example.com" in message
    assert incident.id in message
    assert "HIGH" in message


def test_recovery_notification_contains_recovery_status():
    incident = Incident(service_url="https://example.com")
    incident.recover()

    message = format_incident_recovered(incident)

    assert "INCIDENT RECOVERED" in message
    assert incident.id in message
    assert "RECOVERED" in message
    assert "Recovered at:" in message
