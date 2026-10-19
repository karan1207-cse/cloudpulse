"""Tests for the health-check module."""

from unittest.mock import patch
from urllib.error import URLError

from cloudpulse.health_check import check_health


def test_healthy_endpoint():
    with patch("cloudpulse.health_check.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.status = 200

        result = check_health("https://example.com")

    assert result["is_healthy"] is True
    assert result["status_code"] == 200
    assert result["error"] is None


def test_network_failure():
    with patch(
        "cloudpulse.health_check.urlopen",
        side_effect=URLError("Connection failed"),
    ):
        result = check_health("https://example.com")

    assert result["is_healthy"] is False
    assert result["error"] is not None
    assert result["response_time_ms"] >= 0


def test_http_error():
    from urllib.error import HTTPError

    with patch(
        "cloudpulse.health_check.urlopen",
        side_effect=HTTPError(
            "https://example.com", 503, "Service Unavailable", {}, None
        ),
    ):
        result = check_health("https://example.com")

    assert result["is_healthy"] is False
    assert result["status_code"] == 503
    assert result["error"] == "HTTP error: 503"


def test_invalid_url():
    result = check_health("ftp://example.com")

    assert result["is_healthy"] is False
    assert result["error"] is not None
    assert result["status_code"] is None
