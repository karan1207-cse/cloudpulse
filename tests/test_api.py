"""Dashboard API: routing, validation, views and the local HTTP server."""

import json
import threading
import urllib.request
from unittest.mock import MagicMock
from urllib.error import HTTPError

import pytest

from cloudpulse.api import DashboardApi
from cloudpulse.api_server import create_server
from cloudpulse.config import load_config
from cloudpulse.errors import StorageError
from cloudpulse.history import HealthCheckHistory
from cloudpulse.incident_storage import InMemoryIncidentStorage
from cloudpulse.monitor import MonitoringService
from conftest import ScriptedChecker

SERVICES = [
    {
        "name": "api",
        "url": "https://api.example.com/health?token=s3cret",
        "failure_threshold": 1,
        "severity": "CRITICAL",
    },
    {"name": "web", "url": "https://web.example.com/"},
    {"name": "new", "url": "https://new.example.com/"},
]


@pytest.fixture
def populated(clock):
    config = load_config({"CLOUDPULSE_SERVICES": json.dumps(SERVICES)})
    history = HealthCheckHistory()
    incidents = InMemoryIncidentStorage()

    def monitor(name, *script):
        service = config.service(name)
        return MonitoringService(
            service_url=service.url,
            service_name=name,
            failure_threshold=service.failure_threshold,
            severity=service.severity,
            history=history,
            incident_storage=incidents,
            checker=ScriptedChecker(clock, *script),
            clock=clock,
        )

    api = monitor("api", False, True, False)
    web = monitor("web", True)
    for _ in range(3):
        api.run()
        clock.advance(seconds=60)
    web.run()
    return DashboardApi(config, history, incidents, clock=clock)


def get(api, path, **query):
    return api.handle("GET", path, {k: [str(v)] for k, v in query.items()})


def test_health(populated):
    status, body = get(populated, "/api/health")

    assert status == 200
    assert body["status"] == "ok"
    assert body["time"].endswith("+00:00")


def test_overview_totals(populated):
    status, body = get(populated, "/api/overview")

    assert status == 200
    assert body["totals"] == {
        "services": 3,
        "healthy": 1,
        "unhealthy": 1,
        "unknown": 1,
        "open_incidents": 1,
    }
    assert len(body["recent_recoveries"]) == 1
    assert (
        body["recent_checks"][0]["checked_at"]
        >= body["recent_checks"][-1]["checked_at"]
    )
    assert "s3cret" not in json.dumps(body)


def test_services_list(populated):
    _, body = get(populated, "/api/services")
    by_name = {s["name"]: s for s in body["items"]}

    assert by_name["api"]["status"] == "unhealthy"
    assert by_name["api"]["consecutive_failures"] == 1
    assert by_name["api"]["current_incident"]["severity"] == "CRITICAL"
    assert by_name["api"]["url"] == "https://api.example.com/health"
    assert by_name["web"]["status"] == "healthy"
    assert by_name["web"]["last_response_time_ms"] == 12.5
    assert by_name["new"]["status"] == "unknown"
    assert by_name["new"]["last_checked_at"] is None


def test_service_detail(populated):
    status, body = get(populated, "/api/services/api", limit=2)

    assert status == 200
    assert body["service"]["name"] == "api"
    assert len(body["checks"]) == 2
    assert {i["status"] for i in body["incidents"]} == {"OPEN", "RECOVERED"}
    assert "s3cret" not in json.dumps(body)


def test_service_checks(populated):
    _, body = get(populated, "/api/services/api/checks")

    assert [c["is_healthy"] for c in body["items"]] == [False, True, False]
    assert set(body["items"][0]) <= {
        "id",
        "service",
        "url",
        "final_url",
        "checked_at",
        "is_healthy",
        "status_code",
        "response_time_ms",
        "error",
        "error_type",
        "redirects",
    }


def test_incident_list_filters_sort_and_pagination(populated):
    _, body = get(populated, "/api/incidents")
    assert body["total"] == 2
    assert body["items"][0]["created_at"] > body["items"][1]["created_at"]

    _, body = get(populated, "/api/incidents", status="open")
    assert [i["status"] for i in body["items"]] == ["OPEN"]

    _, body = get(populated, "/api/incidents", severity="LOW")
    assert body["total"] == 0 and body["items"] == []

    _, body = get(
        populated, "/api/incidents", sort="created_at", order="asc", page_size=1, page=2
    )
    assert body["page"] == 2 and body["total_pages"] == 2
    assert body["items"][0]["status"] == "OPEN"

    _, body = get(populated, "/api/incidents", service="web")
    assert body["total"] == 0


def test_incident_detail(populated):
    _, listing = get(populated, "/api/incidents")
    incident_id = listing["items"][0]["id"]

    status, body = get(populated, f"/api/incidents/{incident_id}")

    assert status == 200
    assert body["id"] == incident_id
    assert body["service_url"] == "https://api.example.com/health"


@pytest.mark.parametrize(
    ("path", "query", "status"),
    [
        ("/api/nothing", {}, 404),
        ("/other", {}, 404),
        ("/api/services/missing", {}, 404),
        ("/api/services/bad%20name", {}, 400),
        ("/api/services/api/checks", {"limit": "0"}, 400),
        ("/api/services/api/checks", {"limit": "100000"}, 400),
        ("/api/services/api/checks", {"limit": "abc"}, 400),
        ("/api/incidents", {"status": "CLOSED"}, 400),
        ("/api/incidents", {"severity": "URGENT"}, 400),
        ("/api/incidents", {"sort": "password"}, 400),
        ("/api/incidents", {"page_size": "101"}, 400),
        ("/api/incidents", {"page": "-1"}, 400),
        ("/api/incidents", {"unknown": "1"}, 400),
        ("/api/incidents", {"service": "../etc"}, 400),
        ("/api/incidents/not_an-id!", {}, 400),
        ("/api/incidents/abc", {}, 404),
        ("/api/health", {"x": "1"}, 400),
    ],
)
def test_validation_errors(populated, path, query, status):
    code, body = get(populated, path, **query)

    assert code == status
    assert body["error"]
    assert body["message"]


def test_repeated_parameter_rejected(populated):
    code, _ = populated.handle(
        "GET", "/api/incidents", {"status": ["OPEN", "RECOVERED"]}
    )

    assert code == 400


def test_unsupported_methods_rejected(populated):
    # POST /api/services is now a (permission-checked) write route; methods a
    # route does not support still get 405.
    assert populated.handle("POST", "/api/health", {})[0] == 405
    assert populated.handle("PATCH", "/api/services", {})[0] == 405
    assert populated.handle("POST", "/api/services", {})[0] == 403


def test_storage_errors_become_503_without_details(clock):
    incidents = MagicMock()
    incidents.list_incidents.side_effect = StorageError("table arn:aws:... missing")
    api = DashboardApi(load_config({}), HealthCheckHistory(), incidents, clock=clock)

    status, body = get(api, "/api/incidents")

    assert status == 503
    assert "arn" not in json.dumps(body)


def test_unexpected_errors_become_500(clock):
    incidents = MagicMock()
    incidents.list_incidents.side_effect = KeyError("secret")
    api = DashboardApi(load_config({}), HealthCheckHistory(), incidents, clock=clock)

    status, body = get(api, "/api/incidents")

    assert status == 500
    assert "secret" not in json.dumps(body)


# -- local HTTP server -----------------------------------------------------------


@pytest.fixture
def server(populated, tmp_path):
    static = tmp_path / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>CloudPulse</title>")
    (static / "assets" / "app.js").write_text("console.log('ok')")
    (tmp_path / "secret.txt").write_text("do not serve")
    httpd = create_server(
        populated,
        "127.0.0.1",
        0,
        allowed_origins=("http://localhost:5173",),
        static_dir=static,
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def fetch(url, method="GET", headers=None):
    request = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


@pytest.mark.integration
def test_server_serves_api_with_security_headers(server):
    status, headers, body = fetch(f"{server}/api/overview")

    assert status == 200
    assert json.loads(body)["totals"]["services"] == 3
    assert headers["Content-Type"].startswith("application/json")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in headers


@pytest.mark.integration
def test_server_cors_only_for_allowed_origin(server):
    _, allowed, _ = fetch(
        f"{server}/api/health", headers={"Origin": "http://localhost:5173"}
    )
    _, denied, _ = fetch(
        f"{server}/api/health", headers={"Origin": "https://evil.example"}
    )

    assert allowed["Access-Control-Allow-Origin"] == "http://localhost:5173"
    assert "Access-Control-Allow-Origin" not in denied


@pytest.mark.integration
def test_server_preflight_and_methods(server):
    status, headers, _ = fetch(
        f"{server}/api/health",
        method="OPTIONS",
        headers={"Origin": "http://localhost:5173"},
    )
    assert status == 204
    assert "GET" in headers["Access-Control-Allow-Methods"]

    assert fetch(f"{server}/api/health", method="POST")[0] == 405
    assert fetch(f"{server}/api/health", method="DELETE")[0] == 405


@pytest.mark.integration
def test_server_validation_error_status(server):
    status, _, body = fetch(f"{server}/api/incidents?status=NOPE")

    assert status == 400
    assert json.loads(body)["error"] == "invalid_parameter"


@pytest.mark.integration
def test_server_static_files_and_spa_fallback(server):
    status, headers, body = fetch(f"{server}/")
    assert status == 200 and b"CloudPulse" in body
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]

    status, headers, _ = fetch(f"{server}/assets/app.js")
    assert status == 200 and "immutable" in headers["Cache-Control"]

    assert b"CloudPulse" in fetch(f"{server}/incidents")[2]


@pytest.mark.integration
@pytest.mark.parametrize(
    "path", ["/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"]
)
def test_server_blocks_path_traversal(server, path):
    _, _, body = fetch(f"{server}{path}")

    assert b"do not serve" not in body


def test_server_refuses_non_loopback_without_flag(populated):
    with pytest.raises(ValueError, match="no authentication"):
        create_server(populated, "0.0.0.0", 0)


def test_server_requires_built_static_dir(populated, tmp_path):
    with pytest.raises(ValueError, match="index.html"):
        create_server(populated, "127.0.0.1", 0, static_dir=tmp_path)
