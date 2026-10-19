"""Tests for the HTTP health-check engine (no real network unless local)."""

import socket
import ssl
import threading
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError

import pytest

from cloudpulse.health_check import ErrorType, HttpHealthChecker, check_health
from cloudpulse.url_safety import UrlPolicy
from conftest import FakeTransport, http_error


def run(transport, url="https://example.com/health", **kwargs):
    return check_health(url, transport=transport, **kwargs)


def test_result_has_timestamp_service_and_id(clock):
    result = run(FakeTransport(200), clock=clock, service="api")

    assert result["checked_at"] == "2026-03-01T12:00:00+00:00"
    assert result["service"] == "api"
    assert result["id"]
    assert result["is_healthy"] is True
    assert result["error_type"] is None


def test_service_defaults_to_url():
    assert run(FakeTransport(200))["service"] == "https://example.com/health"


@pytest.mark.parametrize("status", [200, 204, 301, 399])
def test_default_accepted_range_is_200_to_399(status):
    assert run(FakeTransport(status))["is_healthy"] is True


def test_status_outside_custom_range_is_unhealthy():
    result = run(FakeTransport(204), accepted_status=(200, 200))

    assert result["is_healthy"] is False
    assert result["error_type"] == ErrorType.HTTP_STATUS
    assert result["error"] == "HTTP error: 204"


def test_http_error_inside_custom_range_is_healthy():
    result = run(
        FakeTransport(http_error("https://x", 404)), accepted_status=(200, 404)
    )

    assert result["is_healthy"] is True
    assert result["status_code"] == 404


def test_http_5xx_is_unhealthy_with_status():
    result = run(FakeTransport(http_error("https://x", 500)))

    assert result["is_healthy"] is False
    assert result["status_code"] == 500
    assert result["error_type"] == "HTTP_STATUS"


@pytest.mark.parametrize(
    ("exc", "error_type"),
    [
        (URLError(socket.gaierror(-2, "Name or service not known")), "DNS_ERROR"),
        (
            URLError(ConnectionRefusedError(111, "Connection refused")),
            "CONNECTION_ERROR",
        ),
        (URLError(TimeoutError("timed out")), "TIMEOUT"),
        (TimeoutError("read timed out"), "TIMEOUT"),
        (URLError(ssl.SSLCertVerificationError("bad cert")), "TLS_ERROR"),
        (RemoteDisconnected("closed"), "PROTOCOL_ERROR"),
        (URLError("Connection failed"), "CONNECTION_ERROR"),
        (ConnectionResetError(104, "reset"), "CONNECTION_ERROR"),
        (RuntimeError("boom"), "UNEXPECTED_ERROR"),
    ],
)
def test_network_errors_are_classified(exc, error_type):
    result = run(FakeTransport(exc))

    assert result["is_healthy"] is False
    assert result["error_type"] == error_type
    assert result["status_code"] is None
    assert result["error"]


def test_unexpected_error_does_not_leak_message():
    result = run(FakeTransport(RuntimeError("password=hunter2")))

    assert "hunter2" not in result["error"]


def test_invalid_url_never_calls_transport():
    transport = FakeTransport(200)

    result = run(transport, url="ftp://example.com")

    assert result["error_type"] == "INVALID_URL"
    assert transport.requests == []


def test_blocked_destination_never_calls_transport():
    transport = FakeTransport(200)

    result = run(transport, url="http://169.254.169.254/latest/meta-data/")

    assert result["error_type"] == "BLOCKED_DESTINATION"
    assert transport.requests == []


@pytest.mark.parametrize("timeout", [0, -1, 61, "5"])
def test_invalid_timeout_is_rejected(timeout):
    result = run(FakeTransport(200), timeout=timeout)

    assert result["is_healthy"] is False
    assert result["error_type"] == "INVALID_URL"


def test_invalid_accepted_range_is_rejected():
    result = run(FakeTransport(200), accepted_status=(500, 200))

    assert result["is_healthy"] is False


def test_redirects_are_followed_and_revalidated():
    transport = FakeTransport(
        http_error("https://example.com/health", 302, "/v2/health"), 200
    )

    result = run(transport)

    assert result["is_healthy"] is True
    assert result["redirects"] == 1
    assert result["final_url"] == "https://example.com/v2/health"
    assert transport.requests[1].full_url == "https://example.com/v2/health"


def test_redirect_to_metadata_endpoint_is_blocked():
    transport = FakeTransport(
        http_error("https://example.com", 302, "http://169.254.169.254/latest/")
    )

    result = run(transport)

    assert result["is_healthy"] is False
    assert result["error_type"] == "BLOCKED_DESTINATION"
    assert len(transport.requests) == 1


def test_redirect_to_non_http_scheme_is_rejected():
    transport = FakeTransport(http_error("https://example.com", 301, "file:///etc"))

    assert run(transport)["error_type"] == "INVALID_URL"


def test_redirect_limit():
    transport = FakeTransport(http_error("https://example.com", 302, "/loop"))

    result = run(transport, max_redirects=2)

    assert result["is_healthy"] is False
    assert result["error_type"] == "REDIRECT_LIMIT"
    assert len(transport.requests) == 3


def test_timeout_budget_shrinks_across_redirects():
    now = [0.0]

    def slow_redirect(request):
        now[0] = 3.0  # the first hop took three seconds
        return http_error("https://example.com", 302, "/next")

    transport = FakeTransport(slow_redirect, 200)

    run(transport, timeout=5.0, monotonic=lambda: now[0])

    assert transport.timeouts[0] == pytest.approx(5.0)
    assert transport.timeouts[1] == pytest.approx(2.0)


def test_exhausted_budget_is_a_timeout():
    now = [0.0]

    def very_slow_redirect(request):
        now[0] = 6.0
        return http_error("https://example.com", 302, "/next")

    result = run(
        FakeTransport(very_slow_redirect, 200), timeout=5.0, monotonic=lambda: now[0]
    )

    assert result["error_type"] == "TIMEOUT"


def test_request_sends_identifying_user_agent_without_secrets():
    transport = FakeTransport(200)

    run(transport)

    request = transport.requests[0]
    assert request.get_method() == "GET"
    assert request.get_header("User-agent").startswith("CloudPulse/")
    assert request.get_header("Authorization") is None


def test_response_url_is_redacted_in_result():
    result = run(FakeTransport(200), url="https://example.com/h?api_key=abc")

    assert "abc" not in result["url"]


def test_checker_passes_configuration(clock):
    transport = FakeTransport(204)
    checker = HttpHealthChecker(transport=transport, clock=clock)

    result = checker.check(
        "https://example.com", service="svc", timeout=2.0, accepted_status=(200, 200)
    )

    assert result["service"] == "svc"
    assert result["is_healthy"] is False
    assert transport.timeouts[0] <= 2.0


# -- real sockets against a controlled local server ----------------------------


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        status = 200 if self.path == "/ok" else 503
        self.send_response(status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.mark.integration
def test_local_server_is_blocked_by_default(local_server):
    result = check_health(f"{local_server}/ok", timeout=2)

    assert result["error_type"] == "BLOCKED_DESTINATION"


@pytest.mark.integration
def test_localhost_name_is_blocked_after_dns_resolution(local_server):
    port = local_server.rsplit(":", 1)[1]

    result = check_health(f"http://localhost:{port}/ok", timeout=2)

    assert result["error_type"] == "BLOCKED_DESTINATION"


@pytest.mark.integration
def test_local_server_with_explicit_allow(local_server):
    policy = UrlPolicy(allow_private=True)

    ok = check_health(f"{local_server}/ok", timeout=2, policy=policy)
    redirected = check_health(f"{local_server}/redirect", timeout=2, policy=policy)
    failing = check_health(f"{local_server}/fail", timeout=2, policy=policy)

    assert ok["is_healthy"] is True
    assert ok["status_code"] == 200
    assert redirected["is_healthy"] is True
    assert redirected["redirects"] == 1
    assert failing["is_healthy"] is False
    assert failing["status_code"] == 503


@pytest.mark.integration
def test_connection_refused_is_classified():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    result = check_health(
        f"http://127.0.0.1:{port}/", timeout=2, policy=UrlPolicy(allow_private=True)
    )

    assert result["error_type"] in ("CONNECTION_ERROR", "TIMEOUT")


@pytest.mark.integration
def test_environment_proxy_is_ignored(local_server, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://10.255.255.1:3128")
    policy = UrlPolicy(allow_private=True)

    result = check_health(f"{local_server}/ok", timeout=2, policy=policy)

    assert result["is_healthy"] is True
