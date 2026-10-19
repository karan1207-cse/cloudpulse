"""HTTP health checks with typed errors and SSRF protection.

Definition of *healthy*: within the configured timeout, the endpoint returns a
final HTTP response (after following at most ``max_redirects`` validated
redirects) whose status code lies inside the accepted range (inclusive,
default ``200``-``399``). Any other outcome is unhealthy and carries an
``error_type`` from :class:`ErrorType`.

Response bodies are never read or stored.
"""

import functools
import http.client
import socket
import ssl
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request
from uuid import uuid4

from cloudpulse.clock import Clock, to_utc_iso, utc_now
from cloudpulse.url_safety import (
    BlockedDestinationError,
    UnsafeURLError,
    UrlPolicy,
    redact_url,
)

USER_AGENT = "CloudPulse/0.2 (+health-check)"
DEFAULT_TIMEOUT = 5.0
MAX_TIMEOUT = 60.0
DEFAULT_ACCEPTED_STATUS = (200, 399)
DEFAULT_MAX_REDIRECTS = 5
MAX_ERROR_LENGTH = 300


class ErrorType(StrEnum):
    """Classification of an unhealthy check."""

    INVALID_URL = "INVALID_URL"
    BLOCKED_DESTINATION = "BLOCKED_DESTINATION"
    DNS_ERROR = "DNS_ERROR"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    TIMEOUT = "TIMEOUT"
    TLS_ERROR = "TLS_ERROR"
    HTTP_STATUS = "HTTP_STATUS"
    REDIRECT_LIMIT = "REDIRECT_LIMIT"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


# --- Guarded transport ------------------------------------------------------


def _guarded_create_connection(policy: UrlPolicy) -> Callable[..., socket.socket]:
    """Build a ``socket.create_connection`` replacement enforcing ``policy``.

    The hostname is resolved once, every address is validated, and the socket
    connects to a validated IP directly. The connected peer is checked again.
    TLS still verifies the certificate against the original hostname because
    ``HTTPSConnection`` wraps the socket with ``server_hostname=self.host``.
    """

    def create(
        address: tuple[str, int],
        timeout: float | None = None,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        host, port = address
        last_error: OSError | None = None
        for ip in policy.check_resolved(host, port):
            try:
                sock = socket.create_connection((ip, port), timeout, source_address)
            except OSError as exc:
                last_error = exc
                continue
            try:
                policy.check_peer(sock.getpeername()[0])
            except BaseException:
                sock.close()
                raise
            return sock
        raise last_error or OSError(f"Could not connect to {host}:{port}")

    return create


class _GuardedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args: Any, policy: UrlPolicy, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _guarded_create_connection(policy)


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args: Any, policy: UrlPolicy, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _guarded_create_connection(policy)


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, policy: UrlPolicy) -> None:
        super().__init__()
        self._policy = policy

    def http_open(self, req: Request) -> http.client.HTTPResponse:
        return self.do_open(
            functools.partial(_GuardedHTTPConnection, policy=self._policy), req
        )


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, policy: UrlPolicy) -> None:
        super().__init__(context=ssl.create_default_context())
        self._policy = policy

    def https_open(self, req: Request) -> http.client.HTTPResponse:
        return self.do_open(
            functools.partial(_GuardedHTTPSConnection, policy=self._policy),
            req,
            context=self._context,
        )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Surface 3xx responses as ``HTTPError`` so redirects are re-validated."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def build_safe_opener(policy: UrlPolicy) -> urllib.request.OpenerDirector:
    """Return an opener that enforces ``policy``, ignores proxies and
    environment proxy settings, and never follows redirects itself."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _GuardedHTTPHandler(policy),
        _GuardedHTTPSHandler(policy),
        _NoRedirectHandler(),
    )


def urlopen(request: Request, timeout: float, policy: UrlPolicy | None = None):
    """Open ``request`` through the guarded opener (default transport).

    The name mirrors :func:`urllib.request.urlopen` so tests can patch
    ``cloudpulse.health_check.urlopen`` to avoid all network access.
    """
    opener = build_safe_opener(policy or UrlPolicy())
    return opener.open(request, timeout=timeout)


Transport = Callable[..., Any]


# --- Checker ----------------------------------------------------------------


def _truncate(message: str) -> str:
    message = " ".join(str(message).split())
    if len(message) > MAX_ERROR_LENGTH:
        return message[: MAX_ERROR_LENGTH - 3] + "..."
    return message


def classify_exception(exc: BaseException) -> tuple[ErrorType, str]:
    """Map an exception raised while checking into ``(ErrorType, message)``."""
    if isinstance(exc, BlockedDestinationError):
        return ErrorType.BLOCKED_DESTINATION, _truncate(str(exc))
    if isinstance(exc, UnsafeURLError):
        return ErrorType.INVALID_URL, _truncate(str(exc))
    if isinstance(exc, URLError) and not isinstance(exc, HTTPError):
        reason = exc.reason
        if isinstance(reason, BaseException):
            error_type, message = classify_exception(reason)
            return error_type, message
        return ErrorType.CONNECTION_ERROR, _truncate(str(exc))
    if isinstance(exc, socket.gaierror):
        return ErrorType.DNS_ERROR, _truncate(f"DNS resolution failed: {exc}")
    if isinstance(exc, TimeoutError | socket.timeout):
        return ErrorType.TIMEOUT, "Request timed out."
    if isinstance(exc, ssl.SSLError | ssl.CertificateError):
        return ErrorType.TLS_ERROR, _truncate(f"TLS error: {exc}")
    if isinstance(exc, http.client.HTTPException):
        return ErrorType.PROTOCOL_ERROR, _truncate(
            f"Invalid HTTP response: {type(exc).__name__}"
        )
    if isinstance(exc, OSError):
        return ErrorType.CONNECTION_ERROR, _truncate(str(exc) or type(exc).__name__)
    if isinstance(exc, ValueError):
        return ErrorType.INVALID_URL, _truncate(str(exc))
    return ErrorType.UNEXPECTED_ERROR, f"Unexpected error: {type(exc).__name__}"


def validate_accepted_status(accepted: tuple[int, int]) -> tuple[int, int]:
    low, high = accepted
    if not (100 <= low <= high <= 599):
        raise ValueError(
            "Accepted status range must satisfy 100 <= low <= high <= 599."
        )
    return low, high


def check_health(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    service: str | None = None,
    policy: UrlPolicy | None = None,
    accepted_status: tuple[int, int] = DEFAULT_ACCEPTED_STATUS,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    clock: Clock = utc_now,
    transport: Transport | None = None,
    monotonic: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Check an HTTP endpoint and return a health-check record.

    Never raises for network or URL problems; they are reported in the
    returned record as ``is_healthy=False`` with ``error`` and ``error_type``.
    """
    policy = policy or UrlPolicy()
    started_at = monotonic()
    result: dict[str, Any] = {
        "id": str(uuid4()),
        "service": service or url,
        "url": redact_url(url),
        "checked_at": to_utc_iso(clock()),
        "is_healthy": False,
        "status_code": None,
        "response_time_ms": 0.0,
        "error": None,
        "error_type": None,
        "redirects": 0,
    }

    def remaining() -> float:
        return timeout - (monotonic() - started_at)

    try:
        if not isinstance(timeout, int | float) or not 0 < timeout <= MAX_TIMEOUT:
            raise ValueError(
                f"Timeout must be greater than zero and at most {MAX_TIMEOUT}s."
            )
        if max_redirects < 0:
            raise ValueError("max_redirects cannot be negative.")
        low, high = validate_accepted_status(accepted_status)

        current_url = url
        while True:
            policy.check_url(current_url)
            budget = remaining()
            if budget <= 0:
                raise TimeoutError("Timeout budget exhausted.")
            request = Request(
                current_url,
                method="GET",
                headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
            )
            location: str | None = None
            try:
                if transport is not None:
                    response_cm = transport(request, timeout=budget)
                else:
                    response_cm = urlopen(request, timeout=budget, policy=policy)
                with response_cm as response:
                    status = int(response.status)
            except HTTPError as exc:
                status = exc.code
                location = exc.headers.get("Location") if exc.headers else None
                exc.close()

            result["status_code"] = status
            if 300 <= status < 400 and location:
                if result["redirects"] >= max_redirects:
                    result["error_type"] = ErrorType.REDIRECT_LIMIT.value
                    result["error"] = f"More than {max_redirects} redirects."
                    break
                result["redirects"] += 1
                current_url = urljoin(current_url, location)
                continue

            result["is_healthy"] = low <= status <= high
            if not result["is_healthy"]:
                result["error_type"] = ErrorType.HTTP_STATUS.value
                result["error"] = f"HTTP error: {status}"
            if current_url != url:
                result["final_url"] = redact_url(current_url)
            break

    except Exception as exc:  # noqa: BLE001 - every failure becomes a result
        error_type, message = classify_exception(exc)
        result["is_healthy"] = False
        result["error_type"] = error_type.value
        result["error"] = message
        result["status_code"] = None

    finally:
        result["response_time_ms"] = round(max(0.0, monotonic() - started_at) * 1000, 2)

    return result


@dataclass
class HttpHealthChecker:
    """Configured, injectable health checker used by :class:`MonitoringService`."""

    policy: UrlPolicy = field(default_factory=UrlPolicy)
    transport: Transport | None = None
    clock: Clock = utc_now
    max_redirects: int = DEFAULT_MAX_REDIRECTS

    def check(
        self,
        url: str,
        *,
        service: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        accepted_status: tuple[int, int] = DEFAULT_ACCEPTED_STATUS,
    ) -> dict[str, Any]:
        return check_health(
            url,
            timeout,
            service=service,
            policy=self.policy,
            accepted_status=accepted_status,
            max_redirects=self.max_redirects,
            clock=self.clock,
            transport=self.transport,
        )
