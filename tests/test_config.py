"""Configuration loading and validation."""

import json
from pathlib import Path

import pytest

from cloudpulse.config import MAX_SERVICES, load_config
from cloudpulse.errors import ConfigError
from cloudpulse.history import HealthCheckHistory, recent_for_service

SERVICE = {"name": "api", "url": "https://api.example.com/health"}


def env_with(services=None, **extra):
    env = {}
    if services is not None:
        env["CLOUDPULSE_SERVICES"] = json.dumps(services)
    env.update(extra)
    return env


def test_defaults_are_safe_for_local_use():
    config = load_config({})

    assert config.environment == "local"
    assert config.backend == "local"
    assert config.allow_private_targets is False
    assert config.services == ()
    assert config.api_host == "127.0.0.1"
    assert config.sns_topic_arn is None


def test_inline_services_with_defaults():
    config = load_config(env_with([SERVICE], CLOUDPULSE_FAILURE_THRESHOLD="4"))

    [service] = config.services
    assert service.name == "api"
    assert service.failure_threshold == 4
    assert service.timeout_seconds == 5.0
    assert service.accepted_status == (200, 399)
    assert service.severity == "HIGH"


def test_services_file(tmp_path):
    path = tmp_path / "services.json"
    path.write_text(
        json.dumps(
            {
                "services": [
                    {
                        **SERVICE,
                        "severity": "CRITICAL",
                        "accepted_status": [200, 204],
                        "escalation_threshold": 10,
                    }
                ]
            }
        )
    )

    [service] = load_config({}, services_file=path).services

    assert service.severity == "CRITICAL"
    assert service.accepted_status == (200, 204)
    assert service.escalation_threshold == 10


def test_example_services_file_is_valid():
    config = load_config(
        {}, services_file=Path(__file__).parents[1] / "services.example.json"
    )

    assert len(config.services) >= 1


@pytest.mark.parametrize(
    ("service", "message"),
    [
        ({"url": "https://x.example.com"}, "name is required"),
        ({**SERVICE, "name": "bad name!"}, "name must be"),
        ({**SERVICE, "url": "ftp://x"}, "url"),
        ({**SERVICE, "url": "http://169.254.169.254/"}, "blocked"),
        ({**SERVICE, "url": "https://u:p@example.com"}, "credentials"),
        ({**SERVICE, "timeout_seconds": 0}, "timeout_seconds"),
        ({**SERVICE, "timeout_seconds": 31}, "timeout_seconds"),
        ({**SERVICE, "timeout_seconds": True}, "timeout_seconds"),
        ({**SERVICE, "failure_threshold": 0}, "failure_threshold"),
        ({**SERVICE, "failure_threshold": "3"}, "failure_threshold"),
        ({**SERVICE, "severity": "URGENT"}, "severity"),
        ({**SERVICE, "accepted_status": [399, 200]}, "accepted_status"),
        ({**SERVICE, "accepted_status": [200]}, "accepted_status"),
        ({**SERVICE, "escalation_threshold": 2}, "escalation_threshold"),
        ({**SERVICE, "enabled": "yes"}, "enabled"),
        ({**SERVICE, "timeuot": 3}, "unknown keys"),
        ("https://x", "must be an object"),
    ],
)
def test_invalid_services_are_rejected(service, message):
    with pytest.raises(ConfigError, match=message):
        load_config(env_with([service]))


def test_duplicate_names_rejected():
    with pytest.raises(ConfigError, match="Duplicate"):
        load_config(env_with([SERVICE, SERVICE]))


def test_service_count_is_limited():
    services = [
        {"name": f"s{i}", "url": f"https://s{i}.example.com"}
        for i in range(MAX_SERVICES + 1)
    ]

    with pytest.raises(ConfigError, match="At most"):
        load_config(env_with(services))


def test_private_targets_can_be_allowed_locally_only():
    local = load_config(
        env_with(
            [{**SERVICE, "url": "http://127.0.0.1:8080/"}],
            CLOUDPULSE_ALLOW_PRIVATE_TARGETS="true",
        )
    )
    assert local.allow_private_targets is True

    with pytest.raises(ConfigError, match="production"):
        load_config(
            {"CLOUDPULSE_ENV": "production", "CLOUDPULSE_ALLOW_PRIVATE_TARGETS": "true"}
        )
    with pytest.raises(ConfigError, match="production"):
        load_config(
            {"CLOUDPULSE_ENV": "production"}, overrides={"allow_private_targets": True}
        )


def test_aws_backend_requires_tables():
    with pytest.raises(ConfigError, match="CLOUDPULSE_STATE_TABLE"):
        load_config(
            {
                "CLOUDPULSE_BACKEND": "aws",
                "CLOUDPULSE_HEALTH_TABLE": "h",
                "CLOUDPULSE_INCIDENTS_TABLE": "i",
            }
        )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("CLOUDPULSE_ENV", "staging"),
        ("CLOUDPULSE_BACKEND", "s3"),
        ("CLOUDPULSE_TIMEOUT_SECONDS", "fast"),
        ("CLOUDPULSE_TIMEOUT_SECONDS", "0"),
        ("CLOUDPULSE_FAILURE_THRESHOLD", "1.5"),
        ("CLOUDPULSE_ALLOW_PRIVATE_TARGETS", "maybe"),
        ("CLOUDPULSE_SNS_TOPIC_ARN", "arn:aws:s3:::bucket"),
        ("CLOUDPULSE_CORS_ORIGINS", "*"),
        ("CLOUDPULSE_CORS_ORIGINS", "http://ok.example.com,javascript:x"),
        ("CLOUDPULSE_API_PORT", "70000"),
        ("CLOUDPULSE_LOG_LEVEL", "LOUD"),
        ("CLOUDPULSE_SERVICES", "{not json"),
    ],
)
def test_invalid_environment_values(key, value):
    with pytest.raises(ConfigError):
        load_config({key: value})


def test_both_service_sources_rejected(tmp_path):
    with pytest.raises(ConfigError, match="only one"):
        load_config(
            {
                "CLOUDPULSE_SERVICES": "[]",
                "CLOUDPULSE_SERVICES_FILE": str(tmp_path / "x.json"),
            }
        )


def test_missing_services_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config({}, services_file=tmp_path / "missing.json")


def test_valid_sns_arn_is_accepted():
    arn = "arn:aws:sns:ap-south-1:123456789012:cloudpulse-alerts"

    assert load_config({"CLOUDPULSE_SNS_TOPIC_ARN": arn}).sns_topic_arn == arn


def test_public_dict_redacts_url():
    [service] = load_config(
        env_with([{**SERVICE, "url": "https://a.example.com/h?key=s"}])
    ).services

    assert service.to_public_dict()["url"] == "https://a.example.com/h"


def test_recent_ordering_breaks_timestamp_ties_by_insertion():
    # Regression: equal timestamps returned the oldest insertion first.
    history = HealthCheckHistory()
    history.add(
        {"id": "first", "service": "api", "checked_at": "2026-01-01T00:00:00+00:00"}
    )
    history.add(
        {"id": "second", "service": "api", "checked_at": "2026-01-01T00:00:00+00:00"}
    )

    assert history.get_recent("api", 1)[0]["id"] == "second"
    assert recent_for_service(history.records, "api", 0) == []
