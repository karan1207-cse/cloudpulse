"""Notifier implementations, sanitisation and failure isolation."""

import json
import logging
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from cloudpulse.errors import NotificationError
from cloudpulse.incident_manager import Incident
from cloudpulse.notifications import (
    CompositeNotifier,
    EventType,
    LoggingNotifier,
    NotificationEvent,
    RecordingNotifier,
    SnsNotifier,
    deliver,
    format_incident_escalated,
)


def make_incident(**kwargs):
    defaults = {
        "service_url": "https://user:pw@api.example.com/health?token=s3cret",
        "service_name": "api",
        "failure_count": 3,
        "last_error": "HTTP error: 503",
        "last_error_type": "HTTP_STATUS",
        "last_status_code": 503,
    }
    return Incident(**{**defaults, **kwargs})


@pytest.mark.parametrize("event_type", list(EventType))
def test_messages_never_contain_secrets_from_urls(event_type):
    incident = make_incident()
    incident.recover()
    event = NotificationEvent(event_type, incident)

    for text in (event.message, event.subject):
        assert "s3cret" not in text
        assert "pw@" not in text
    assert "https://api.example.com/health" in event.message


def test_open_message_contains_diagnostics():
    message = NotificationEvent(EventType.INCIDENT_OPENED, make_incident()).message

    assert "Consecutive failed checks: 3" in message
    assert "Last HTTP status: 503" in message
    assert "HTTP_STATUS" in message


def test_recovery_message_does_not_claim_a_fix():
    incident = make_incident()
    incident.recover()

    message = NotificationEvent(EventType.INCIDENT_RECOVERED, incident).message

    assert "fixed" not in message.lower()
    assert "root cause is not determined" in message


def test_escalation_message():
    incident = make_incident(
        severity="CRITICAL", diagnostics={"escalated_from": "HIGH"}
    )

    assert "ESCALATED (from HIGH)" in format_incident_escalated(incident)


def test_subject_is_sns_safe():
    incident = make_incident(service_name="a" * 200)

    subject = NotificationEvent(EventType.INCIDENT_OPENED, incident).subject

    assert len(subject) <= 100
    assert "\n" not in subject
    subject.encode("ascii")


def test_long_errors_are_truncated():
    incident = make_incident(last_error="x" * 5000)

    assert len(NotificationEvent(EventType.INCIDENT_OPENED, incident).message) < 1000


def test_logging_notifier(caplog):
    with caplog.at_level(logging.WARNING):
        LoggingNotifier().send(
            NotificationEvent(EventType.INCIDENT_OPENED, make_incident())
        )

    assert "INCIDENT OPENED" in caplog.text


@pytest.mark.aws
def test_sns_notifier_publishes_to_topic(sns_topic):
    client, arn = sns_topic
    sent = []
    original = client.publish

    def spy(**kwargs):
        sent.append(kwargs)
        return original(**kwargs)

    client.publish = spy
    SnsNotifier(arn, client=client).send(
        NotificationEvent(EventType.INCIDENT_OPENED, make_incident())
    )

    assert sent[0]["TopicArn"] == arn
    assert sent[0]["Subject"].startswith("[HIGH] CloudPulse INCIDENT OPENED")
    attrs = sent[0]["MessageAttributes"]
    assert attrs["event_type"]["StringValue"] == "INCIDENT_OPENED"
    json.dumps(sent[0])  # payload is plain data


@pytest.mark.aws
def test_sns_publish_to_missing_topic_raises_notification_error(sns_topic):
    client, arn = sns_topic
    notifier = SnsNotifier(
        arn.replace("cloudpulse-test-alerts", "missing"), client=client
    )

    with pytest.raises(NotificationError):
        notifier.send(NotificationEvent(EventType.INCIDENT_OPENED, make_incident()))


def test_sns_client_errors_are_wrapped():
    client = MagicMock()
    client.publish.side_effect = ClientError(
        {"Error": {"Code": "AuthorizationError", "Message": "denied"}}, "Publish"
    )

    with pytest.raises(NotificationError, match="AuthorizationError"):
        SnsNotifier("arn:aws:sns:us-east-1:123456789012:t", client=client).send(
            NotificationEvent(EventType.INCIDENT_OPENED, make_incident())
        )


def test_sns_requires_topic_arn():
    with pytest.raises(ValueError):
        SnsNotifier("not-an-arn", client=MagicMock())


def test_composite_continues_after_failure(failing_notifier):
    recorder = RecordingNotifier()
    composite = CompositeNotifier(failing_notifier, recorder)

    with pytest.raises(NotificationError, match="smtp down"):
        composite.send(NotificationEvent(EventType.INCIDENT_OPENED, make_incident()))

    assert len(recorder.events) == 1


def test_deliver_swallows_and_reports_failures(failing_notifier, caplog):
    event = NotificationEvent(EventType.INCIDENT_OPENED, make_incident())

    with caplog.at_level(logging.ERROR):
        error = deliver(failing_notifier, event)

    assert error == "smtp down"
    assert "delivery failed" in caplog.text
    assert deliver(None, event) is None
    assert deliver(RecordingNotifier(), event) is None
