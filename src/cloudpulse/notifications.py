
"""Notification message formatting for CloudPulse."""

from cloudpulse.incident_manager import Incident


def format_incident_opened(incident: Incident) -> str:
    """Format a message for a newly opened incident."""
    return (
        f"[{incident.severity}] INCIDENT OPENED\n"
        f"Service: {incident.service_url}\n"
        f"Incident ID: {incident.id}\n"
        f"Status: {incident.status}\n"
        f"Created at: {incident.created_at.isoformat()}"
    )


def format_incident_recovered(incident: Incident) -> str:
    """Format a message for a recovered incident."""
    return (
        f"[{incident.severity}] INCIDENT RECOVERED\n"
        f"Service: {incident.service_url}\n"
        f"Incident ID: {incident.id}\n"
        f"Status: {incident.status}\n"
        f"Recovered at: {incident.recovered_at.isoformat()}"
        if incident.recovered_at is not None
        else (
            f"[{incident.severity}] INCIDENT RECOVERED\n"
            f"Service: {incident.service_url}\n"
            f"Incident ID: {incident.id}\n"
            f"Status: {incident.status}"
        )
    )
