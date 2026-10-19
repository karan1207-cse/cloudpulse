"""Local HTTP server for the dashboard API (and, optionally, the built UI).

Security defaults:

* binds to ``127.0.0.1``; binding elsewhere requires ``allow_remote=True``
  because the API has no authentication;
* only ``GET``/``HEAD``/``OPTIONS``; request bodies are ignored;
* CORS only for the explicit origins in ``CLOUDPULSE_CORS_ORIGINS``;
* ``nosniff``, ``no-store`` and a restrictive CSP on every response;
* static files are confined to the configured directory.
"""

import ipaddress
import json
import logging
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from cloudpulse.api import DashboardApi

logger = logging.getLogger(__name__)

MAX_PATH_LENGTH = 2048
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


def is_loopback_host(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_handler(
    api: DashboardApi, allowed_origins: tuple[str, ...], static_dir: Path | None
) -> type[BaseHTTPRequestHandler]:
    static_root = static_dir.resolve() if static_dir else None

    class Handler(BaseHTTPRequestHandler):
        server_version = "CloudPulse"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            logger.info("%s %s", self.address_string(), format % args)

        def _cors(self) -> dict[str, str]:
            origin = self.headers.get("Origin")
            if origin and origin in allowed_origins:
                return {
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": "GET, OPTIONS",
                    "Access-Control-Allow-Headers": "Accept, Content-Type",
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
        ) -> None:
            self.send_response(status)
            headers = {**SECURITY_HEADERS, **self._cors()}
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

        def _json(self, status: int, payload: dict[str, Any], head_only: bool) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self._send(
                status, body, "application/json; charset=utf-8", head_only=head_only
            )

        def do_OPTIONS(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT)
            for key, value in {**SECURITY_HEADERS, **self._cors()}.items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self, head_only: bool = False) -> None:
            if len(self.path) > MAX_PATH_LENGTH:
                self._json(
                    414,
                    {"error": "uri_too_long", "message": "URI too long."},
                    head_only,
                )
                return
            parts = urlsplit(self.path)
            path = unquote(parts.path)
            if path == "/api" or path.startswith("/api/"):
                query = parse_qs(parts.query, keep_blank_values=True)
                status, payload = api.handle("GET", path, query)
                self._json(status, payload, head_only)
                return
            if static_root is not None:
                self._static(path, head_only)
                return
            self._json(
                404, {"error": "not_found", "message": "Unknown endpoint."}, head_only
            )

        def do_HEAD(self) -> None:
            self.do_GET(head_only=True)

        def _method_not_allowed(self) -> None:
            self._json(
                405,
                {"error": "method_not_allowed", "message": "Only GET is supported."},
                False,
            )

        do_POST = do_PUT = do_PATCH = do_DELETE = _method_not_allowed

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
) -> ThreadingHTTPServer:
    if not allow_remote and not is_loopback_host(host):
        raise ValueError(
            f"Refusing to bind to {host}: the API has no authentication. "
            "Use 127.0.0.1, or pass --allow-remote behind your own auth proxy."
        )
    if static_dir is not None and not (static_dir / "index.html").is_file():
        raise ValueError(
            f"{static_dir} does not contain index.html (run npm run build)."
        )
    handler = make_handler(api, allowed_origins, static_dir)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
