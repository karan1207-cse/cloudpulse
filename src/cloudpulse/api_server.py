"""Local HTTP server for the dashboard API (and, optionally, the built UI).

Security model:

* Binds to ``127.0.0.1`` by default.
* **Writes** (add/edit/delete services, check now):
  - without ``CLOUDPULSE_ADMIN_TOKEN``: only from a loopback client of a
    loopback-bound server;
  - with a token: every write must send ``Authorization: Bearer <token>``,
    compared in constant time. ``--allow-remote`` (binding to a non-loopback
    address) is refused unless a token is configured.
* Cross-site protection for writes: ``Content-Type: application/json`` is
  required (forces a CORS preflight), the ``Origin`` header — when present —
  must be this server or an allowed origin, and on a loopback-bound server
  the ``Host`` header must be a loopback name (blocks DNS rebinding).
* Request bodies are limited to 16 KiB; writes and checks are rate limited
  per client address.
* CORS only for the explicit origins in ``CLOUDPULSE_CORS_ORIGINS``;
  ``nosniff``, ``no-store`` and a restrictive CSP on every response; static
  files are confined to the configured directory.
"""

import hmac
import ipaddress
import json
import logging
import mimetypes
import threading
import time
from collections import deque
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from cloudpulse.api import WRITE_METHODS, DashboardApi, RequestContext

logger = logging.getLogger(__name__)

MAX_PATH_LENGTH = 2048
MAX_BODY_BYTES = 16 * 1024
WRITE_LIMIT_PER_MINUTE = 60
CHECK_LIMIT_PER_MINUTE = 20
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
)
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


def is_loopback_host(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class RateLimiter:
    """Sliding-window limit of ``limit`` events per ``window`` seconds per key."""

    def __init__(
        self,
        limit: int,
        window: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self.monotonic = monotonic
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> float | None:
        """Record an event; return seconds to wait if over the limit."""
        now = self.monotonic()
        with self._lock:
            events = self._events.setdefault(key, deque())
            while events and now - events[0] >= self.window:
                events.popleft()
            if len(events) >= self.limit:
                return max(1.0, self.window - (now - events[0]))
            events.append(now)
            if len(self._events) > 10_000:  # bound memory
                self._events = {k: v for k, v in self._events.items() if v}
            return None


def token_matches(header: str | None, token: str) -> bool:
    if not header or not header.startswith("Bearer "):
        return False
    supplied = header[len("Bearer ") :].strip().encode("utf-8")
    return hmac.compare_digest(supplied, token.encode("utf-8"))


def make_handler(
    api: DashboardApi,
    allowed_origins: tuple[str, ...],
    static_dir: Path | None,
    *,
    admin_token: str | None = None,
    loopback_only: bool = True,
    write_limiter: RateLimiter | None = None,
    check_limiter: RateLimiter | None = None,
) -> type[BaseHTTPRequestHandler]:
    static_root = static_dir.resolve() if static_dir else None
    write_limiter = write_limiter or RateLimiter(WRITE_LIMIT_PER_MINUTE)
    check_limiter = check_limiter or RateLimiter(CHECK_LIMIT_PER_MINUTE)

    class Handler(BaseHTTPRequestHandler):
        server_version = "CloudPulse"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            # The request line never contains the token (it is a header).
            logger.info("%s %s", self.address_string(), format % args)

        # -- helpers ----------------------------------------------------------

        def _cors(self) -> dict[str, str]:
            origin = self.headers.get("Origin")
            if origin and origin in allowed_origins:
                return {
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": ("GET, POST, PUT, DELETE, OPTIONS"),
                    "Access-Control-Allow-Headers": (
                        "Accept, Content-Type, Authorization"
                    ),
                    "Access-Control-Max-Age": "600",
                    "Vary": "Origin",
                }
            return {"Vary": "Origin"}

        def _send(
            self,
            status: int,
            body: bytes,
            content_type: str,
            *,
            head_only: bool = False,
            cache: str | None = None,
            extra: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            headers = {**SECURITY_HEADERS, **self._cors(), **(extra or {})}
            if cache:
                headers["Cache-Control"] = cache
            if content_type.startswith("text/html"):
                headers["Content-Security-Policy"] = CSP
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def _json(
            self,
            status: int,
            payload: dict[str, Any],
            head_only: bool = False,
            extra: dict[str, str] | None = None,
        ) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self._send(
                status,
                body,
                "application/json; charset=utf-8",
                head_only=head_only,
                extra=extra,
            )

        def _error(
            self,
            status: int,
            code: str,
            message: str,
            extra: dict[str, str] | None = None,
        ) -> None:
            self._json(status, {"error": code, "message": message}, extra=extra)

        def _client_is_loopback(self) -> bool:
            return is_loopback_host(self.client_address[0])

        def _host_allowed(self) -> bool:
            if not loopback_only:
                return True
            host = (self.headers.get("Host") or "").strip().lower()
            if host.startswith("["):
                name = host.split("]")[0] + "]"
            else:
                name = host.rsplit(":", 1)[0] if ":" in host else host
            return name in LOOPBACK_NAMES

        def _context(self) -> RequestContext:
            if admin_token:
                return RequestContext(
                    can_write=token_matches(
                        self.headers.get("Authorization"), admin_token
                    ),
                    auth_required=True,
                )
            return RequestContext(
                can_write=loopback_only and self._client_is_loopback(),
                auth_required=False,
            )

        def _same_origin_or_allowed(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True  # non-browser client (curl, tests)
            if origin in allowed_origins:
                return True
            host = self.headers.get("Host") or ""
            return origin in (f"http://{host}", f"https://{host}")

        # -- verbs ------------------------------------------------------------

        def do_OPTIONS(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT)
            for key, value in {**SECURITY_HEADERS, **self._cors()}.items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self, head_only: bool = False) -> None:
            self._dispatch("GET", head_only=head_only)

        def do_HEAD(self) -> None:
            self.do_GET(head_only=True)

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def do_PATCH(self) -> None:
            self._error(405, "method_not_allowed", "PATCH is not supported.")

        def _read_body(self) -> tuple[bool, Any]:
            """Return (ok, parsed body). On failure an error was already sent."""
            length_header = self.headers.get("Content-Length")
            if self.headers.get("Transfer-Encoding"):
                self._error(411, "length_required", "Send a Content-Length header.")
                return False, None
            if length_header is None:
                return True, None
            try:
                length = int(length_header)
            except ValueError:
                self._error(400, "invalid_body", "Invalid Content-Length.")
                return False, None
            if length < 0 or length > MAX_BODY_BYTES:
                self._error(
                    413,
                    "body_too_large",
                    f"Request body must be at most {MAX_BODY_BYTES} bytes.",
                )
                return False, None
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return True, None
            try:
                return True, json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._error(400, "invalid_body", "The request body is not valid JSON.")
                return False, None

        def _dispatch(self, method: str, head_only: bool = False) -> None:
            if len(self.path) > MAX_PATH_LENGTH:
                self._error(414, "uri_too_long", "URI too long.")
                return
            parts = urlsplit(self.path)
            path = unquote(parts.path)
            is_api = path == "/api" or path.startswith("/api/")

            if is_api and not self._host_allowed():
                self._error(
                    421,
                    "misdirected_request",
                    "This server only answers requests for localhost.",
                )
                return

            if not is_api:
                if method != "GET":
                    self._error(405, "method_not_allowed", "Only GET is supported.")
                elif static_root is not None:
                    self._static(path, head_only)
                else:
                    self._json(
                        404,
                        {"error": "not_found", "message": "Unknown endpoint."},
                        head_only,
                    )
                return

            body = None
            if method in WRITE_METHODS:
                if not self._same_origin_or_allowed():
                    self._error(
                        403,
                        "cross_site_blocked",
                        "Cross-site requests are not allowed.",
                    )
                    return
                content_type = (self.headers.get("Content-Type") or "").split(";")[0]
                has_body = (self.headers.get("Content-Length") or "0").strip() != "0"
                if has_body and content_type.strip() != "application/json":
                    self._error(
                        415,
                        "unsupported_media_type",
                        "Send the request body as application/json.",
                    )
                    return
                is_check = path.rstrip("/").endswith("/check") or (
                    path.rstrip("/") == "/api/checks"
                )
                limiter = check_limiter if is_check else write_limiter
                retry_after = limiter.check(self.client_address[0])
                if retry_after is not None:
                    self._error(
                        429,
                        "rate_limited",
                        "Too many requests. Please wait a moment and try again.",
                        extra={"Retry-After": str(int(retry_after))},
                    )
                    return
                ok, body = self._read_body()
                if not ok:
                    return

            query = parse_qs(parts.query, keep_blank_values=True)
            status, payload, headers = api.handle_full(
                method, path, query, body, self._context()
            )
            if status == 401:
                logger.warning(
                    "Rejected write without a valid admin token from %s",
                    self.client_address[0],
                )
            self._json(status, payload, head_only, extra=headers)

        def _static(self, path: str, head_only: bool) -> None:
            assert static_root is not None
            relative = path.lstrip("/") or "index.html"
            candidate = (static_root / relative).resolve()
            if not candidate.is_relative_to(static_root):
                self._json(
                    404, {"error": "not_found", "message": "Not found."}, head_only
                )
                return
            if not candidate.is_file():
                # Single-page app: unknown paths fall back to index.html.
                candidate = static_root / "index.html"
                if not candidate.is_file():
                    self._json(
                        404, {"error": "not_found", "message": "Not found."}, head_only
                    )
                    return
            content_type = (
                mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            )
            if content_type.startswith("text/") or content_type.endswith("javascript"):
                content_type += "; charset=utf-8"
            cache = (
                "public, max-age=31536000, immutable" if "/assets/" in path else None
            )
            self._send(
                200,
                candidate.read_bytes(),
                content_type,
                head_only=head_only,
                cache=cache,
            )

    return Handler


def create_server(
    api: DashboardApi,
    host: str,
    port: int,
    *,
    allowed_origins: tuple[str, ...] = (),
    static_dir: Path | None = None,
    allow_remote: bool = False,
    admin_token: str | None = None,
    write_limiter: RateLimiter | None = None,
    check_limiter: RateLimiter | None = None,
) -> ThreadingHTTPServer:
    loopback = is_loopback_host(host)
    if not loopback and not allow_remote:
        raise ValueError(
            f"Refusing to bind to {host}: the API has no authentication for "
            "reads. Use 127.0.0.1, or pass --allow-remote (requires "
            "CLOUDPULSE_ADMIN_TOKEN) behind your own access controls."
        )
    if allow_remote and not admin_token:
        raise ValueError(
            "--allow-remote requires CLOUDPULSE_ADMIN_TOKEN so that changes "
            "(adding, editing, deleting and checking services) need a token."
        )
    if static_dir is not None and not (static_dir / "index.html").is_file():
        raise ValueError(
            f"{static_dir} does not contain index.html (run npm run build)."
        )
    handler = make_handler(
        api,
        allowed_origins,
        static_dir,
        admin_token=admin_token,
        loopback_only=loopback and not allow_remote,
        write_limiter=write_limiter,
        check_limiter=check_limiter,
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
