"""Write API: create/update/delete/check-now, validation, SSRF, auth, limits."""

import json
import threading
import urllib.request
from urllib.error import HTTPError

import pytest

from cloudpulse.api import DashboardApi, RequestContext
from cloudpulse.api_server import RateLimiter, create_server, token_matches
from cloudpulse.config import load_config
from cloudpulse.errors import ConfigError
from cloudpulse.factory import Dependencies
from cloudpulse.history import HealthCheckHistory
from cloudpulse.incident_storage import InMemoryIncidentStorage
from cloudpulse.notifications import RecordingNotifier
from cloudpulse.scheduler import MonitorRunner
from cloudpulse.service_registry import ServiceRegistry
from cloudpulse.service_store import InMemoryServiceStore
from conftest import ScriptedChecker

WRITE = RequestContext(can_write=True)
NO_WRITE = RequestContext(can_write=False)
NEEDS_TOKEN = RequestContext(can_write=False, auth_required=True)
TOKEN = "t" * 40


@pytest.fixture
def setup(fake_dns, clock):
    config = load_config(
        {
            "CLOUDPULSE_SERVICES": json.dumps(
                [{"name": "fixed", "url": "https://fixed.example.com/"}]
            )
        }
    )
    registry = ServiceRegistry(config, InMemoryServiceStore(clock))
    checker = ScriptedChecker(clock, False, True)
    deps = Dependencies(
        history=HealthCheckHistory(),
        incidents=InMemoryIncidentStorage(),
        notifier=RecordingNotifier(),
        checker=checker,  # type: ignore[arg-type]
        clock=clock,
    )
    runner = MonitorRunner(registry, deps, config)
    api = DashboardApi(
        config, deps.history, deps.incidents, clock, registry=registry, runner=runner
    )
    return api, registry, deps


def call(api, method, path, body=None, context=WRITE, **query):
    return api.handle(
        method, path, {k: [str(v)] for k, v in query.items()}, body, context
    )


NEW = {"name": "shop", "url": "https://shop.example.com/"}


def test_create_returns_summary(setup):
    api, registry, _ = setup

    status, body = call(api, "POST", "/api/services", NEW)

    assert status == 201
    assert body["service"]["name"] == "shop"
    assert body["service"]["status"] == "unknown"
    assert body["service"]["source"] == "dashboard"
    assert body["service"]["editable"] is True
    assert body["warnings"] == []
    assert registry.get("shop") is not None
    listed = call(api, "GET", "/api/services")[1]["items"]
    assert [s["name"] for s in listed] == ["fixed", "shop"]
    assert listed[0]["editable"] is False


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"name": "shop", "url": "not a url"}, "url"),
        ({"name": "shop", "url": "http://10.1.2.3/"}, "url"),
        ({"name": "shop", "url": "http://localhost/"}, "url"),
        ({"name": "bad name", "url": "https://x.example.com/"}, "name"),
        ({"name": "fixed", "url": "https://x.example.com/"}, "name"),
        ({**NEW, "timeout_seconds": 99}, "timeout_seconds"),
    ],
)
def test_create_validation_errors(setup, body, field):
    api, registry, _ = setup

    status, payload = call(api, "POST", "/api/services", body)

    assert status == 400
    assert payload["error"] == "validation_error"
    assert payload["field"] == field
    assert payload["message"]
    assert registry.get("shop") is None


def test_create_rejects_non_object_body(setup):
    api, _, _ = setup

    for body in (None, [], "x", 3):
        status, payload = call(api, "POST", "/api/services", body)
        assert status == 400
        assert payload["error"] == "invalid_body"


def test_create_rejects_dns_to_metadata(setup, fake_dns):
    api, _, _ = setup
    fake_dns.mapping["meta.example.com"] = ["169.254.169.254"]

    status, payload = call(
        api, "POST", "/api/services", {"name": "m", "url": "http://meta.example.com/"}
    )

    assert status == 400
    assert "169.254.169.254" in payload["message"]


def test_update_and_delete(setup):
    api, _registry, _ = setup
    call(api, "POST", "/api/services", NEW)

    status, body = call(
        api,
        "PUT",
        "/api/services/shop",
        {**NEW, "failure_threshold": 9, "severity": "LOW"},
    )
    assert status == 200
    assert body["service"]["failure_threshold"] == 9
    assert body["service"]["severity"] == "LOW"

    status, body = call(api, "PUT", "/api/services/shop", {**NEW, "name": "renamed"})
    assert status == 400 and body["field"] == "name"

    assert call(api, "DELETE", "/api/services/shop") == (200, {"deleted": "shop"})
    assert call(api, "DELETE", "/api/services/shop")[0] == 404
    assert call(api, "PUT", "/api/services/shop", NEW)[0] == 404


def test_config_services_cannot_be_changed(setup):
    api, _, _ = setup

    assert call(api, "DELETE", "/api/services/fixed")[0] == 409
    status, body = call(
        api, "PUT", "/api/services/fixed", {"url": "https://x.example.com/"}
    )
    assert status == 409
    assert body["error"] == "read_only"


def test_writes_require_permission(setup):
    api, registry, _ = setup

    status, body = call(api, "POST", "/api/services", NEW, context=NO_WRITE)
    assert status == 403
    assert body["error"] == "forbidden"

    status, body = call(api, "POST", "/api/services", NEW, context=NEEDS_TOKEN)
    assert status == 401
    assert body["error"] == "unauthorized"

    for method, path in (
        ("PUT", "/api/services/fixed"),
        ("DELETE", "/api/services/fixed"),
        ("POST", "/api/services/fixed/check"),
        ("POST", "/api/checks"),
    ):
        assert call(api, method, path, {}, context=NO_WRITE)[0] == 403
    assert registry.get("shop") is None


def test_reads_do_not_need_permission(setup):
    api, _, _ = setup

    assert call(api, "GET", "/api/services", context=NO_WRITE)[0] == 200


def test_check_now_runs_and_updates_summary(setup):
    api, _, deps = setup
    call(api, "POST", "/api/services", NEW)

    status, body = call(api, "POST", "/api/services/shop/check")

    assert status == 200
    assert body["outcome"]["is_healthy"] is False
    assert body["outcome"]["transition"] == "FAILURE_RECORDED"
    assert body["service"]["status"] == "unhealthy"
    assert body["service"]["consecutive_failures"] == 1
    assert deps.history.get_recent("shop", 5)[0]["service"] == "shop"


def test_check_now_ignores_duplicate_guard(setup):
    api, _, _ = setup
    api.runner.config = api.runner.config.__class__(
        **{**api.runner.config.__dict__, "min_check_interval_seconds": 600}
    )

    first = call(api, "POST", "/api/services/fixed/check")[1]
    second = call(api, "POST", "/api/services/fixed/check")[1]

    assert first["outcome"]["skipped"] is False
    assert second["outcome"]["skipped"] is False


def test_check_now_unknown_service(setup):
    api, _, _ = setup

    assert call(api, "POST", "/api/services/nope/check")[0] == 404
    assert call(api, "POST", "/api/services/bad%20name/check")[0] == 400


def test_check_all(setup):
    api, _, _ = setup
    call(api, "POST", "/api/services", NEW)

    status, body = call(api, "POST", "/api/checks")

    assert status == 200
    assert body["checked"] == 2
    assert {s["service"] for s in body["services"]} == {"fixed", "shop"}


def test_check_unavailable_without_runner(fake_dns, clock):
    config = load_config({})
    api = DashboardApi(config, HealthCheckHistory(), InMemoryIncidentStorage(), clock)

    status, body = call(api, "POST", "/api/checks")
    assert status == 503
    assert body["error"] == "checks_unavailable"


def test_health_reports_capabilities(setup):
    api, _, _ = setup

    _, body = call(api, "GET", "/api/health")
    assert body["can_write"] is True
    assert body["can_check"] is True
    assert body["auth_required"] is False
    assert body["limits"] == {"max_services": 50, "services": 1}
    assert body["defaults"]["accepted_status"] == [200, 399]
    assert body["scheduler"] == {"enabled": False}

    _, body = call(api, "GET", "/api/health", context=NEEDS_TOKEN)
    assert body["can_write"] is False
    assert body["auth_required"] is True


def test_deleted_service_open_incident_not_counted_on_overview(setup):
    api, _, _deps = setup
    call(api, "POST", "/api/services", {**NEW, "failure_threshold": 1})
    call(api, "POST", "/api/services/shop/check")  # fails -> incident opened
    assert call(api, "GET", "/api/overview")[1]["totals"]["open_incidents"] == 1

    call(api, "DELETE", "/api/services/shop")

    overview = call(api, "GET", "/api/overview")[1]
    assert overview["totals"]["open_incidents"] == 0
    # The incident itself is kept in history.
    assert call(api, "GET", "/api/incidents")[1]["total"] == 1


# -- HTTP layer -------------------------------------------------------------------


def start(api, **kwargs):
    server = create_server(api, "127.0.0.1", 0, **kwargs)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def send(url, method="GET", body=None, headers=None, raw=None):
    data = (
        raw
        if raw is not None
        else (json.dumps(body).encode() if body is not None else None)
    )
    hdrs = {"Content-Type": "application/json"} if data is not None else {}
    hdrs.update(headers or {})
    request = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return (
                response.status,
                json.loads(response.read() or b"{}"),
                dict(response.headers),
            )
    except HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}"), dict(exc.headers)


@pytest.fixture
def local_server(setup):
    api, _, _ = setup
    server, base = start(api, allowed_origins=("http://localhost:5173",))
    yield base
    server.shutdown()
    server.server_close()


@pytest.mark.integration
def test_local_writes_work_from_loopback(local_server):
    status, body, _ = send(f"{local_server}/api/services", "POST", NEW)
    assert status == 201

    status, body, _ = send(
        f"{local_server}/api/services/shop", "PUT", {**NEW, "enabled": False}
    )
    assert status == 200 and body["service"]["enabled"] is False

    status, body, _ = send(f"{local_server}/api/services/shop/check", "POST", {})
    assert status == 200

    status, _, _ = send(f"{local_server}/api/services/shop", "DELETE")
    assert status == 200


@pytest.mark.integration
def test_cross_site_writes_are_blocked(local_server):
    status, body, _ = send(
        f"{local_server}/api/services",
        "POST",
        NEW,
        headers={"Origin": "https://evil.example"},
    )
    assert status == 403
    assert body["error"] == "cross_site_blocked"

    allowed, _, _ = send(
        f"{local_server}/api/services",
        "POST",
        NEW,
        headers={"Origin": "http://localhost:5173"},
    )
    assert allowed == 201


@pytest.mark.integration
def test_dns_rebinding_host_header_is_rejected(local_server):
    status, body, _ = send(
        f"{local_server}/api/services", headers={"Host": "attacker.example:80"}
    )

    assert status == 421
    assert body["error"] == "misdirected_request"


@pytest.mark.integration
def test_write_requires_json_and_valid_body(local_server):
    status, body, _ = send(
        f"{local_server}/api/services",
        "POST",
        raw=b"name=x",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert status == 415

    status, body, _ = send(f"{local_server}/api/services", "POST", raw=b"{not json")
    assert status == 400 and body["error"] == "invalid_body"

    status, body, _ = send(
        f"{local_server}/api/services", "POST", raw=b"{" + b" " * 20_000 + b"}"
    )
    assert status == 413


@pytest.mark.integration
def test_preflight_allows_write_methods_for_allowed_origin(local_server):
    request = urllib.request.Request(
        f"{local_server}/api/services",
        method="OPTIONS",
        headers={"Origin": "http://localhost:5173"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert "POST" in response.headers["Access-Control-Allow-Methods"]
        assert "Authorization" in response.headers["Access-Control-Allow-Headers"]


@pytest.mark.integration
def test_rate_limit(setup):
    api, _, _ = setup
    server, base = start(
        api, write_limiter=RateLimiter(2), check_limiter=RateLimiter(1)
    )
    try:
        codes = [
            send(
                f"{base}/api/services",
                "POST",
                {"name": f"s{i}", "url": "https://x.example.com/"},
            )[0]
            for i in range(3)
        ]
        assert codes == [201, 201, 429]
        first = send(f"{base}/api/checks", "POST", {})[0]
        status, _body, headers = send(f"{base}/api/checks", "POST", {})
        assert first == 200
        assert status == 429
        assert int(headers["Retry-After"]) >= 1
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.integration
def test_admin_token_required_when_configured(setup):
    api, registry, _ = setup
    server, base = start(api, admin_token=TOKEN)
    try:
        status, _body, headers = send(f"{base}/api/services", "POST", NEW)
        assert status == 401
        assert headers["WWW-Authenticate"].startswith("Bearer")

        wrong = {"Authorization": "Bearer " + "x" * 40}
        assert send(f"{base}/api/services", "POST", NEW, headers=wrong)[0] == 401
        assert (
            send(f"{base}/api/services", "POST", NEW, headers={"Authorization": TOKEN})[
                0
            ]
            == 401
        )
        assert registry.get("shop") is None

        good = {"Authorization": f"Bearer {TOKEN}"}
        assert send(f"{base}/api/services", "POST", NEW, headers=good)[0] == 201

        health = send(f"{base}/api/health")[1]
        assert health["auth_required"] is True and health["can_write"] is False
        assert send(f"{base}/api/services")[0] == 200  # reads stay open
    finally:
        server.shutdown()
        server.server_close()


def test_allow_remote_requires_token(setup):
    api, _, _ = setup

    with pytest.raises(ValueError, match="CLOUDPULSE_ADMIN_TOKEN"):
        create_server(api, "127.0.0.1", 0, allow_remote=True)
    with pytest.raises(ValueError, match="no authentication"):
        create_server(api, "0.0.0.0", 0)


def test_token_comparison():
    assert token_matches(f"Bearer {TOKEN}", TOKEN)
    assert not token_matches(TOKEN, TOKEN)
    assert not token_matches(None, TOKEN)
    assert not token_matches("Bearer short", TOKEN)
    assert not token_matches(f"Basic {TOKEN}", TOKEN)


@pytest.mark.parametrize("token", ["short", "x" * 31, "has space" + "x" * 40])
def test_weak_admin_tokens_rejected(token):
    with pytest.raises(ConfigError, match="CLOUDPULSE_ADMIN_TOKEN"):
        load_config({"CLOUDPULSE_ADMIN_TOKEN": token})


def test_admin_token_never_in_repr():
    config = load_config({"CLOUDPULSE_ADMIN_TOKEN": TOKEN})

    assert config.admin_token == TOKEN
    assert TOKEN not in repr(config)


def test_rate_limiter_window():
    now = [0.0]
    limiter = RateLimiter(2, window=60, monotonic=lambda: now[0])

    assert limiter.check("a") is None
    assert limiter.check("a") is None
    assert limiter.check("a") == pytest.approx(60)
    assert limiter.check("b") is None
    now[0] = 61
    assert limiter.check("a") is None
