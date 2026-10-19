"""Shared fixtures: deterministic clocks, fake HTTP transports, mocked AWS."""

import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError

import pytest

from cloudpulse.clock import FixedClock

# Make sure no test can ever reach real AWS, even by accident.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
for _var in ("AWS_PROFILE", "CLOUDPULSE_SNS_TOPIC_ARN"):
    os.environ.pop(_var, None)

HEALTH_TABLE = "cloudpulse-test-health-checks"
INCIDENTS_TABLE = "cloudpulse-test-incidents"
STATE_TABLE = "cloudpulse-test-monitor-state"


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2026, 3, 1, 12, 0, tzinfo=UTC))


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.closed = False

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True


def http_error(url: str, status: int, location: str | None = None) -> HTTPError:
    headers = {"Location": location} if location else {}
    return HTTPError(url, status, "error", headers, None)  # type: ignore[arg-type]


class FakeTransport:
    """Scripted transport: each call pops the next outcome.

    An outcome is an int (status), an exception instance, or a callable
    ``(request) -> int | Exception``.
    """

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[Any] = []
        self.timeouts: list[float] = []

    def __call__(self, request: Any, timeout: float, **_: Any) -> FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if callable(outcome) and not isinstance(outcome, BaseException):
            outcome = outcome(request)
        if isinstance(outcome, BaseException):
            raise outcome
        return FakeResponse(int(outcome))


@pytest.fixture
def transport_factory() -> Callable[..., FakeTransport]:
    return FakeTransport


def healthy_result(service: str = "https://example.com", **extra: Any) -> dict:
    return {
        "url": service,
        "service": service,
        "is_healthy": True,
        "status_code": 200,
        "response_time_ms": 12.5,
        "error": None,
        "error_type": None,
        **extra,
    }


def failed_result(service: str = "https://example.com", **extra: Any) -> dict:
    return {
        "url": service,
        "service": service,
        "is_healthy": False,
        "status_code": 503,
        "response_time_ms": 20.0,
        "error": "HTTP error: 503",
        "error_type": "HTTP_STATUS",
        **extra,
    }


class ScriptedChecker:
    """Stand-in for HttpHealthChecker returning scripted results."""

    def __init__(self, clock: Callable[[], datetime], *healthy: bool) -> None:
        self.clock = clock
        self.script = list(healthy)
        self.calls = 0

    def check(self, url: str, *, service: str | None = None, **_: Any) -> dict:
        self.calls += 1
        ok = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        builder = healthy_result if ok else failed_result
        return builder(
            service or url,
            url=url,
            id=f"check-{self.calls}",
            checked_at=self.clock().isoformat(),
        )


def create_tables(dynamodb: Any) -> None:
    """Create tables matching infra/terraform/dynamodb.tf."""
    dynamodb.create_table(
        TableName=HEALTH_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "service", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "service", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
    )
    dynamodb.create_table(
        TableName=INCIDENTS_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "id", "AttributeType": "S"},
            {"AttributeName": "service_name", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "service-created-index",
                "KeySchema": [
                    {"AttributeName": "service_name", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "status-created-index",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )
    dynamodb.create_table(
        TableName=STATE_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "service", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "service", "KeyType": "HASH"}],
    )


@pytest.fixture
def aws_mock() -> Iterator[None]:
    moto = pytest.importorskip("moto")
    with moto.mock_aws():
        yield


@pytest.fixture
def dynamodb(aws_mock: None) -> Any:
    import boto3

    resource = boto3.resource("dynamodb", region_name="us-east-1")
    create_tables(resource)
    return resource


@pytest.fixture
def sns_topic(aws_mock: None) -> tuple[Any, str]:
    import boto3

    client = boto3.client("sns", region_name="us-east-1")
    arn = client.create_topic(Name="cloudpulse-test-alerts")["TopicArn"]
    return client, arn


@pytest.fixture
def failing_notifier() -> MagicMock:
    notifier = MagicMock()
    notifier.send.side_effect = RuntimeError("smtp down")
    return notifier


__all__ = [
    "HEALTH_TABLE",
    "INCIDENTS_TABLE",
    "STATE_TABLE",
    "FakeTransport",
    "ScriptedChecker",
    "URLError",
    "failed_result",
    "healthy_result",
    "http_error",
]
