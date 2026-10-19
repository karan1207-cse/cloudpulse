"""Notification formatting and delivery for CloudPulse.

Notifications are sent only on state *transitions* — an incident opening,
escalating to CRITICAL, or recovering — never for every failed check.

Messages contain only: service name, a redacted URL (no credentials, query
string or fragment), severity, status, incident id, timestamps, failure count
and the classified error. Response bodies and headers are never included.
"""

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from cloudpulse.aws_common import BotoCoreError, ClientError, error_code, require_boto3
from cloudpulse.errors import NotificationError
from cloudpulse.incident_manager import Incident
from cloudpulse.url_safety import redact_url

logger = logging.getLogger(__name__)

SNS_SUBJECT_LIMIT = 100


class EventType(StrEnum):
    INCIDENT_OPENED = "INCIDENT_OPENED"
    INCIDENT_ESCALATED = "INCIDENT_ESCALATED"
    INCIDENT_RECOVERED = "INCIDENT_RECOVERED"


@dataclass(frozen=True)
class NotificationEvent:
    """A transition worth telling a human about."""

    event_type: EventType
    incident: Incident

    @property
    def subject(self) -> str:
        label = {
            EventType.INCIDENT_OPENED: "INCIDENT OPENED",
            EventType.INCIDENT_ESCALATED: "INCIDENT ESCALATED",
            EventType.INCIDENT_RECOVERED: "INCIDENT RECOVERED",
        }[self.event_type]
        incident = self.incident
        subject = f"[{incident.severity}] CloudPulse {label}: {incident.service_name}"
        # SNS subjects must be ASCII, without newlines, at most 100 chars.
        subject = subject.encode("ascii", "replace").decode("ascii")
        subject = " ".join(subject.split())
        return subject[:SNS_SUBJECT_LIMIT]

    @property
    def message(self) -> str:
        if self.event_type is EventType.INCIDENT_RECOVERED:
            return format_incident_recovered(self.incident)
        if self.event_type is EventType.INCIDENT_ESCALATED:
            return format_incident_escalated(self.incident)
        return format_incident_opened(self.incident)

    def attributes(self) -> dict[str, str]:
        return {
            "event_type": self.event_type.value,
            "severity": self.incident.severity,
            "service": str(self.incident.service_name)[:256],
        }


def _common_lines(incident: Incident) -> list[str]:
    lines = [f"Service: {incident.service_name}"]
    url = redact_url(incident.service_url)
    if url != incident.service_name:
        lines.append(f"URL: {url}")
    lines += [f"Incident ID: {incident.id}", f"Status: {incident.status}"]
    return lines


def _failure_lines(incident: Incident) -> list[str]:
    lines = [f"Consecutive failed checks: {incident.failure_count}"]
    if incident.last_error_type:
        lines.append(f"Last error type: {incident.last_error_type}")
    if incident.last_status_code is not None:
        lines.append(f"Last HTTP status: {incident.last_status_code}")
    if incident.last_error:
        lines.append(f"Last error: {incident.last_error[:300]}")
    return lines


def format_incident_opened(incident: Incident) -> str:
    """Format a message for a newly opened incident."""
    lines = [
        f"[{incident.severity}] INCIDENT OPENED",
        *_common_lines(incident),
        f"Created at: {incident.created_at.isoformat()}",
    ]
    if incident.failure_count:
        lines += _failure_lines(incident)
    return "\n".join(lines)


def format_incident_escalated(incident: Incident) -> str:
    """Format a message for an incident escalated to CRITICAL."""
    previous = incident.diagnostics.get("escalated_from", "unknown")
    lines = [
        f"[{incident.severity}] INCIDENT ESCALATED (from {previous})",
        *_common_lines(incident),
        f"Created at: {incident.created_at.isoformat()}",
    ]
    if incident.escalated_at is not None:
        lines.append(f"Escalated at: {incident.escalated_at.isoformat()}")
    lines += _failure_lines(incident)
    return "\n".join(lines)


def format_incident_recovered(incident: Incident) -> str:
    """Format a message for a recovered incident.

    "Recovered" means the service passed a health check again; CloudPulse does
    not claim the underlying cause was fixed.
    """
    lines = [f"[{incident.severity}] INCIDENT RECOVERED", *_common_lines(incident)]
    lines.append(f"Created at: {incident.created_at.isoformat()}")
    if incident.recovered_at is not None:
        lines.append(f"Recovered at: {incident.recovered_at.isoformat()}")
        duration = (incident.recovered_at - incident.created_at).total_seconds()
        lines.append(f"Open for: {duration:.0f}s")
    lines.append("The service passed a health check; the root cause is not determined.")
    return "\n".join(lines)


class Notifier(Protocol):
    """Deliver a notification. Implementations may raise NotificationError."""

    def send(self, event: NotificationEvent) -> None: ...


class LoggingNotifier:
    """Write notifications to the ``cloudpulse.notifications`` logger."""

    def __init__(self, log: logging.Logger | None = None) -> None:
        self.log = log or logger

    def send(self, event: NotificationEvent) -> None:
        self.log.warning(
            "%s", event.message, extra={"event_type": event.event_type.value}
        )


@dataclass
class RecordingNotifier:
    """Keep notifications in memory (tests and dry runs)."""

    events: list[NotificationEvent] = field(default_factory=list)

    def send(self, event: NotificationEvent) -> None:
        self.events.append(event)


class SnsNotifier:
    """Publish notifications to an Amazon SNS topic.

    Requires ``sns:Publish`` on exactly this topic ARN. Credentials come from
    the standard AWS chain (Lambda execution role, SSO profile, ...).
    """

    def __init__(
        self, topic_arn: str, client: Any = None, region_name: str | None = None
    ) -> None:
        if not topic_arn.startswith("arn:") or ":sns:" not in topic_arn:
            raise ValueError("topic_arn must be an SNS topic ARN.")
        self.topic_arn = topic_arn
        if client is None:
            client = require_boto3().client("sns", region_name=region_name)
        self.client = client

    def send(self, event: NotificationEvent) -> None:
        try:
            self.client.publish(
                TopicArn=self.topic_arn,
                Subject=event.subject,
                Message=event.message,
                MessageAttributes={
                    key: {"DataType": "String", "StringValue": value}
                    for key, value in event.attributes().items()
                },
            )
        except ClientError as exc:
            raise NotificationError(f"SNS publish failed: {error_code(exc)}") from exc
        except BotoCoreError as exc:
            raise NotificationError(
                f"SNS publish failed: {type(exc).__name__}"
            ) from exc


class CompositeNotifier:
    """Send to several notifiers; one failing does not stop the others."""

    def __init__(self, *notifiers: Notifier) -> None:
        self.notifiers = list(notifiers)

    def send(self, event: NotificationEvent) -> None:
        errors: list[str] = []
        for notifier in self.notifiers:
            try:
                notifier.send(event)
            except Exception as exc:  # noqa: BLE001 - aggregate and re-raise
                errors.append(f"{type(notifier).__name__}: {exc}")
        if errors:
            raise NotificationError("; ".join(errors))


def deliver(notifier: Notifier | None, event: NotificationEvent) -> str | None:
    """Send ``event`` and swallow failures; return an error string or ``None``.

    Incident state is always persisted before this is called, so a failed
    notification can never corrupt or roll back incident state.
    """
    if notifier is None:
        return None
    try:
        notifier.send(event)
    except Exception as exc:  # noqa: BLE001 - delivery must never break monitoring
        message = str(exc) or type(exc).__name__
        logger.error(
            "Notification delivery failed for incident %s: %s",
            event.incident.id,
            message,
        )
        return message
    return None
