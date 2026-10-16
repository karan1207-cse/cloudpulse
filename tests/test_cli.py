
"""Tests for the CloudPulse command-line interface."""

from unittest.mock import patch

from cloudpulse.cli import main


def test_cli_prints_successful_health_check(capsys):
    with (
        patch("sys.argv", ["cloudpulse", "https://example.com"]),
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
            "cloudpulse.cli.MonitoringService.__post_init__"
        ),
    ):
        main()

    output = capsys.readouterr().out

    assert "Health Check Result:" in output
    assert '"is_healthy": true' in output
    assert "Stored checks: 0" in output
