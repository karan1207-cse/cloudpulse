"""Lambda handler tests with moto-mocked DynamoDB and SNS (no real AWS)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from cloudpulse import aws_lambda
from cloudpulse.errors import ConfigError
from cloudpulse.observability import emf_record
from conftest import (
    HEALTH_TABLE,
    INCIDENTS_TABLE,
    STATE_TABLE,
    FakeTransport,
    http_error,
)

pytestmark = pytest.mark.aws

SCHEDULED_EVENT = {
    "version": "0",
    "id": "53dc4d37-cffa-4f76-80c9-8b7d4a4d2eaa",
    "detail-type": "Scheduled Event",
    "source": "aws.events",
    "account": "123456789012",
    "time": "2026-03-01T12:00:00Z",
    "region": "us-east-1",
    "resources": ["arn:aws:events:us-east-1:123456789012:rule/cloudpulse"],
    "detail": {},
}


class Context:
    aws_request_id = "req-1"

    def __init__(self, remaining_ms=60_000):
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self):
        return self.remaining_ms


@pytest.fixture
def lambda_env(dynamodb, sns_topic, monkeypatch):
    client, arn = sns_topic
    monkeypatch.setenv("CLOUDPULSE_ENV", "production")
    monkeypatch.setenv("CLOUDPULSE_BACKEND", "aws")
    monkeypatch.setenv("CLOUDPULSE_HEALTH_TABLE", HEALTH_TABLE)
    monkeypatch.setenv("CLOUDPULSE_INCIDENTS_TABLE", INCIDENTS_TABLE)
    monkeypatch.setenv("CLOUDPULSE_STATE_TABLE", STATE_TABLE)
    monkeypatch.setenv("CLOUDPULSE_SNS_TOPIC_ARN", arn)
    monkeypatch.setenv("CLOUDPULSE_MIN_CHECK_INTERVAL_SECONDS", "30")
    monkeypatch.setenv(
        "CLOUDPULSE_SERVICES",
        json.dumps(
            [
                {
                    "name": "api",
                    "url": "https://api.example.com/health",
                    "failure_threshold": 1,
                },
                {
                    "name": "web",
                    "url": "https://web.example.com/",
                    "timeout_seconds": 2,
                },
            ]
        ),
    )
    aws_lambda._cache.clear()
    published = []
    original_publish = client.publish

    def spy(**kwargs):
        published.append(kwargs)
        return original_publish(**kwargs)

    with patch("cloudpulse.notifications.require_boto3") as boto:
        sns_client = MagicMock(wraps=client)
        sns_client.publish.side_effect = spy
        boto.return_value.client.return_value = sns_client
        yield {"dynamodb": dynamodb, "published": published, "topic": arn}
    aws_lambda._cache.clear()


def network(*outcomes):
    return patch(
        "cloudpulse.health_check.urlopen", side_effect=FakeTransport(*outcomes)
    )


def test_scheduled_event_checks_all_services(lambda_env, capsys):
    with network(http_error("https://api", 503), 200):
        result = aws_lambda.handler(SCHEDULED_EVENT, Context())

    assert result["ok"] is True
    assert result["checked"] == 2
    assert result["unhealthy"] == 1
    assert result["metrics"]["IncidentsOpened"] == 1
    assert result["request_id"] == "req-1"

    table = lambda_env["dynamodb"].Table(INCIDENTS_TABLE)
    [incident] = table.scan()["Items"]
    assert incident["status"] == "OPEN"
    assert incident["service_name"] == "api"
    assert len(lambda_env["dynamodb"].Table(HEALTH_TABLE).scan()["Items"]) == 2

    [message] = lambda_env["published"]
    assert message["TopicArn"] == lambda_env["topic"]
    assert "INCIDENT OPENED" in message["Subject"]

    emf_lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    emf = next(doc for doc in emf_lines if "_aws" in doc)
    assert emf["IncidentsOpened"] == 1
    assert emf["Environment"] == "production"


def test_duplicate_delivery_is_ignored(lambda_env):
    with network(http_error("https://api", 503), 200):
        aws_lambda.handler(SCHEDULED_EVENT, Context())
        again = aws_lambda.handler(SCHEDULED_EVENT, Context())

    assert again["checked"] == 0
    assert again["metrics"]["ServicesSkipped"] == 2
    assert len(lambda_env["published"]) == 1
    assert len(lambda_env["dynamodb"].Table(INCIDENTS_TABLE).scan()["Items"]) == 1


def test_recovery_across_invocations(lambda_env, monkeypatch):
    monkeypatch.setenv("CLOUDPULSE_MIN_CHECK_INTERVAL_SECONDS", "0")
    with network(http_error("https://api", 503)):
        aws_lambda.handler({"services": ["api"]}, Context())
    aws_lambda._cache.clear()  # cold start
    with network(200):
        result = aws_lambda.handler({"services": ["api"]}, Context())

    assert result["metrics"]["IncidentsRecovered"] == 1
    [incident] = lambda_env["dynamodb"].Table(INCIDENTS_TABLE).scan()["Items"]
    assert incident["status"] == "RECOVERED"
    assert [m["Subject"].split()[2] for m in lambda_env["published"]] == [
        "INCIDENT",
        "INCIDENT",
    ]
    assert "RECOVERED" in lambda_env["published"][1]["Subject"]


def test_manual_event_selects_services(lambda_env):
    with network(200):
        result = aws_lambda.handler({"services": ["web"]}, Context())

    assert [s["service"] for s in result["services"]] == ["web"]


@pytest.mark.parametrize(
    "event",
    [
        "not a dict",
        {"services": "api"},
        {"services": ["missing"]},
        {"services": [1]},
        {"unexpected": True},
    ],
)
def test_invalid_events_are_rejected(lambda_env, event):
    result = aws_lambda.handler(event, Context())

    assert result["ok"] is False
    assert result["error"] == "invalid_event"


def test_time_budget_skips_services(lambda_env):
    with network(200):
        result = aws_lambda.handler(SCHEDULED_EVENT, Context(remaining_ms=1000))

    assert result["checked"] == 0
    assert result["skipped_for_time"] == ["api", "web"]


def test_sns_failure_does_not_lose_incident(lambda_env):
    with (
        network(http_error("https://api", 503), 200),
        patch(
            "cloudpulse.notifications.SnsNotifier.send",
            side_effect=RuntimeError("throttled"),
        ),
    ):
        result = aws_lambda.handler(SCHEDULED_EVENT, Context())

    assert result["metrics"]["NotificationFailures"] == 1
    assert result["ok"] is True
    [incident] = lambda_env["dynamodb"].Table(INCIDENTS_TABLE).scan()["Items"]
    assert incident["status"] == "OPEN"


def test_partial_storage_failure_is_reported(lambda_env):
    real = aws_lambda.build_dependencies

    def flaky(config):
        deps = real(config)
        original = deps.incidents.get_state

        def get_state(service):
            if service == "web":
                from cloudpulse.errors import StorageError

                raise StorageError("throttled")
            return original(service)

        deps.incidents.get_state = get_state
        return deps

    with network(200), patch("cloudpulse.aws_lambda.build_dependencies", flaky):
        result = aws_lambda.handler(SCHEDULED_EVENT, Context())

    assert result["ok"] is False
    assert result["checked"] == 1
    assert result["failures"][0]["service"] == "web"


def test_total_failure_raises_for_alarm(lambda_env, monkeypatch):
    monkeypatch.setenv("CLOUDPULSE_STATE_TABLE", "missing-table")

    with network(200), pytest.raises(RuntimeError, match="All 2"):
        aws_lambda.handler(SCHEDULED_EVENT, Context())


def test_invalid_configuration_raises(lambda_env, monkeypatch):
    monkeypatch.setenv("CLOUDPULSE_SERVICES", "{broken")

    with pytest.raises(ConfigError):
        aws_lambda.handler(SCHEDULED_EVENT, Context())


def test_handler_requires_aws_backend(lambda_env, monkeypatch):
    monkeypatch.setenv("CLOUDPULSE_BACKEND", "local")

    with pytest.raises(ConfigError, match="aws"):
        aws_lambda.handler(SCHEDULED_EVENT, Context())


def test_private_targets_blocked_in_lambda(lambda_env, monkeypatch):
    monkeypatch.setenv(
        "CLOUDPULSE_SERVICES",
        json.dumps([{"name": "meta", "url": "http://metadata.example/"}]),
    )

    fake_dns = [(2, 1, 6, "", ("169.254.169.254", 80))]
    with patch("cloudpulse.url_safety.socket.getaddrinfo", return_value=fake_dns):
        aws_lambda._cache.clear()
        result = aws_lambda.handler(SCHEDULED_EVENT, Context())

    assert result["services"][0]["error_type"] == "BLOCKED_DESTINATION"


def test_dependencies_are_reused_on_warm_start(lambda_env):
    with (
        network(200),
        patch(
            "cloudpulse.aws_lambda.build_dependencies",
            wraps=aws_lambda.build_dependencies,
        ) as build,
    ):
        aws_lambda.handler({"services": ["web"]}, Context())
        aws_lambda.handler({"services": ["api"]}, Context())

    assert build.call_count == 1


def test_emf_document_shape():
    doc = emf_record(
        {"ChecksFailed": 2}, dimensions={"Environment": "dev"}, timestamp_ms=1
    )

    assert doc["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "CloudPulse"
    assert doc["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["Environment"]]
    assert doc["ChecksFailed"] == 2
