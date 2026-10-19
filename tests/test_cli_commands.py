"""CLI subcommands and exit codes (network always mocked)."""

import json
from unittest.mock import patch

import pytest

from cloudpulse import cli
from cloudpulse.incident_storage import JsonIncidentStorage
from conftest import FakeTransport, http_error


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    services = tmp_path / "services.json"
    services.write_text(
        json.dumps(
            [
                {
                    "name": "api",
                    "url": "https://api.example.com/health",
                    "failure_threshold": 1,
                },
                {"name": "web", "url": "https://web.example.com/"},
            ]
        )
    )
    for key in list(__import__("os").environ):
        if key.startswith("CLOUDPULSE_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("CLOUDPULSE_SERVICES_FILE", str(services))
    monkeypatch.setenv("CLOUDPULSE_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def fake_network(*outcomes):
    """Patch the module-level transport used by HttpHealthChecker."""
    return patch(
        "cloudpulse.health_check.urlopen", side_effect=FakeTransport(*outcomes)
    )


def test_help_lists_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    for command in ("check", "run", "incidents", "history", "status", "serve"):
        assert command in out


def test_check_healthy_returns_zero(capsys, workspace):
    with fake_network(200):
        code = cli.main(["check", "https://example.com/"])

    assert code == cli.EXIT_OK
    assert "HEALTHY" in capsys.readouterr().out


def test_check_unhealthy_returns_one_and_json(capsys, workspace):
    with fake_network(http_error("https://example.com", 503)):
        code = cli.main(["check", "https://example.com/", "--json"])

    assert code == cli.EXIT_UNHEALTHY
    assert json.loads(capsys.readouterr().out)["status_code"] == 503


def test_check_blocks_private_targets(capsys, workspace):
    code = cli.main(["check", "http://127.0.0.1:9/"])

    assert code == cli.EXIT_UNHEALTHY
    assert "BLOCKED_DESTINATION" in capsys.readouterr().out


def test_invalid_arguments_exit_two(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["check", "https://x", "--timeout", "-1"])
    assert exc.value.code == 2

    with pytest.raises(SystemExit) as exc:
        cli.main(["check", "https://x", "--accepted-status", "500-200"])
    assert exc.value.code == 2


def test_run_persists_and_reports(capsys, workspace):
    with fake_network(http_error("https://api", 500), 200):
        code = cli.main(["run"])

    assert code == cli.EXIT_UNHEALTHY
    out = capsys.readouterr().out
    assert "INCIDENT_OPENED" in out
    storage = JsonIncidentStorage(workspace / "data" / "incidents.json")
    assert storage.count() == 1


def test_run_json_and_single_service(capsys, workspace):
    with fake_network(200):
        code = cli.main(["run", "--service", "web", "--json"])

    assert code == cli.EXIT_OK
    summary = json.loads(capsys.readouterr().out)
    assert summary["checked"] == 1
    assert summary["services"][0]["service"] == "web"


def test_run_unknown_service_is_config_error(capsys, workspace):
    assert cli.main(["run", "--service", "nope"]) == cli.EXIT_USAGE
    assert "Unknown service" in capsys.readouterr().err


def test_run_without_services_is_config_error(capsys, monkeypatch, tmp_path):
    monkeypatch.delenv("CLOUDPULSE_SERVICES_FILE", raising=False)
    monkeypatch.delenv("CLOUDPULSE_SERVICES", raising=False)
    monkeypatch.setenv("CLOUDPULSE_DATA_DIR", str(tmp_path))

    assert cli.main(["run"]) == cli.EXIT_USAGE
    assert "No services configured" in capsys.readouterr().err


def test_corrupt_storage_exits_three(capsys, workspace):
    data = workspace / "data"
    data.mkdir()
    (data / "incidents.json").write_text("{corrupt")

    assert cli.main(["incidents"]) == cli.EXIT_STORAGE
    assert "storage error" in capsys.readouterr().err


def test_status_incidents_history_after_run(capsys, workspace):
    with fake_network(http_error("https://api", 500), 200):
        cli.main(["run"])
    capsys.readouterr()

    assert cli.main(["status"]) == 0
    status = capsys.readouterr().out
    assert "api" in status and "unhealthy" in status and "healthy" in status

    assert cli.main(["incidents", "--status", "open"]) == 0
    assert "OPEN" in capsys.readouterr().out

    assert cli.main(["incidents", "--json", "--severity", "low"]) == 0
    assert json.loads(capsys.readouterr().out) == []

    assert cli.main(["history", "--service", "api", "--json"]) == 0
    [record] = json.loads(capsys.readouterr().out)
    assert record["service"] == "api"
    assert record["error_type"] == "HTTP_STATUS"


def test_empty_listings(capsys, workspace):
    assert cli.main(["incidents"]) == 0
    assert "No incidents" in capsys.readouterr().out
    assert cli.main(["history"]) == 0
    assert "No health checks" in capsys.readouterr().out


def test_config_validate_and_show(capsys, workspace):
    assert cli.main(["config"]) == 0
    assert "Configuration OK: 2 service(s)" in capsys.readouterr().out
    assert cli.main(["config", "show"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["services"][0]["name"] == "api"
    assert shown["sns_topic_configured"] is False


def test_production_refuses_allow_private(capsys, workspace, monkeypatch):
    monkeypatch.setenv("CLOUDPULSE_ENV", "production")

    assert cli.main(["check", "http://127.0.0.1/", "--allow-private"]) == cli.EXIT_USAGE


def test_serve_refuses_public_bind(capsys, workspace):
    assert cli.main(["serve", "--host", "0.0.0.0"]) == cli.EXIT_USAGE
    assert "no authentication" in capsys.readouterr().err


def test_legacy_monitor_rejects_bad_threshold(capsys, workspace):
    assert cli.main(["https://example.com", "--threshold", "0"]) == cli.EXIT_USAGE


def test_legacy_monitor_rejects_unsafe_url(capsys, workspace):
    assert cli.main(["http://169.254.169.254/"]) == cli.EXIT_USAGE


def test_run_loop_until_interrupted(capsys, workspace):
    with (
        fake_network(200),
        patch("cloudpulse.cli.time.sleep", side_effect=KeyboardInterrupt),
    ):
        code = cli.main(["run", "--loop", "--interval", "1"])

    assert code == cli.EXIT_INTERRUPTED
    assert "Round 1" in capsys.readouterr().out


def test_check_ignores_unrelated_services_file(capsys, workspace, monkeypatch):
    # Regression: `check` failed with a config error because the services
    # file contained a loopback URL that only `run` would use.
    bad = workspace / "bad-services.json"
    bad.write_text(json.dumps([{"name": "local", "url": "http://127.0.0.1:1/"}]))
    monkeypatch.setenv("CLOUDPULSE_SERVICES_FILE", str(bad))

    with fake_network(200):
        code = cli.main(["check", "https://example.com/"])

    assert code == cli.EXIT_OK
    assert cli.main(["config"]) == cli.EXIT_USAGE
