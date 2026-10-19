"""Tests for the CloudPulse command-line interface."""

from unittest.mock import patch

from cloudpulse.cli import main


def test_cli_prints_successful_health_check(capsys):
    with (
        patch(
            "sys.argv",
            [
                "cloudpulse",
                "https://example.com",
                "--interval",
                "1",
            ],
        ),
        patch(
            "cloudpulse.cli.MonitoringService.run_check",
            return_value={
                "url": "https://example.com",
                "is_healthy": True,
                "status_code": 200,
                "response_time_ms": 10.0,
                "error": None,
            },
        ),
        patch(
            "cloudpulse.cli.time.sleep",
            side_effect=KeyboardInterrupt,
        ),
    ):
        main()

    output = capsys.readouterr().out

    assert "Monitoring: https://example.com" in output
    assert "Health Check #1" in output
    assert '"is_healthy": true' in output
    assert "CloudPulse monitoring stopped." in output
