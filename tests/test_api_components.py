"""API: check mode on services and full-site component management."""

import io
import json
from urllib.error import HTTPError

import pytest

from cloudpulse.api import DashboardApi, RequestContext
from cloudpulse.config import load_config
from cloudpulse.factory import Dependencies
from cloudpulse.health_check import HttpHealthChecker
from cloudpulse.history import HealthCheckHistory
from cloudpulse.incident_storage import InMemoryIncidentStorage
from cloudpulse.notifications import RecordingNotifier
from cloudpulse.scheduler import MonitorRunner
from cloudpulse.service_registry import ServiceRegistry
from cloudpulse.service_store import InMemoryServiceStore
from conftest import ScriptedChecker

WRITE = RequestContext(can_write=True)
SITE = "https://app.example-site.com/"
BACKEND = "https://expense-api.onrender.com/"


class Site:
    """Tiny scripted site: an SPA whose bundle calls an onrender.com API."""

    def __init__(self, backend_up=True):
        self.backend_up = backend_up
        self.urls = []

    def __call__(self, request, timeout, **_):
        url = request.full_url
        self.urls.append(url)
        pages = {
            SITE: (b'<script src="/app.js"></script>', "text/html"),
            SITE + "app.js": (
                b'axios.create({baseURL:"https://expense-api.onrender.com/api"})',
                "application/javascript",
            ),
        }
        if url == BACKEND:
            if not self.backend_up:
                raise TimeoutError("timed out")
            raise HTTPError(url, 404, "nf", {}, io.BytesIO(b""))
        if url not in pages:
            raise HTTPError(url, 404, "nf", {}, io.BytesIO(b""))
        body, ctype = pages[url]

        class R:
            status = 200

            def __init__(self):
                self.headers = {"Content-Type": ctype}

            def read(self, n=-1):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R()


@pytest.fixture
def setup(fake_dns, clock):
    config = load_config({})
    registry = ServiceRegistry(config, InMemoryServiceStore(clock))
    site = Site()
    deps = Dependencies(
        history=HealthCheckHistory(),
        incidents=InMemoryIncidentStorage(),
        notifier=RecordingNotifier(),
        checker=HttpHealthChecker(transport=site, clock=clock),
        clock=clock,
    )
    runner = MonitorRunner(registry, deps, config)
    api = DashboardApi(
        config, deps.history, deps.incidents, clock, registry=registry, runner=runner
    )
    return api, site, deps


def call(api, method, path, body=None, context=WRITE):
    return api.handle(method, path, {}, body, context)


def add_full(api):
    status, body = call(
        api,
        "POST",
        "/api/services",
        {"name": "expense", "url": SITE, "check_mode": "full"},
    )
    assert status == 201, body
    return body


def test_create_full_service_and_check_now(setup):
    api, site, _ = setup
    created = add_full(api)
    assert created["service"]["check_mode"] == "full"
    assert created["service"]["backend_timeout_seconds"] == 20.0

    status, body = call(api, "POST", "/api/services/expense/check")

    assert status == 200
    assert body["outcome"]["is_healthy"] is True
    assert body["outcome"]["check_mode"] == "full"
    assert BACKEND in site.urls
    site_view = call(api, "GET", "/api/services/expense")[1]["site"]
    assert site_view["check_mode"] == "full"
    assert site_view["discovered_at"] is not None
    assert {c["url"]: c["status"] for c in site_view["components"]} == {
        SITE: "up",
        SITE + "app.js": "up",
        BACKEND: "up",
    }


def test_check_now_reports_failing_backend_in_plain_words(setup):
    api, site, _ = setup
    add_full(api)
    site.backend_up = False

    body = call(api, "POST", "/api/services/expense/check")[1]

    assert body["outcome"]["is_healthy"] is False
    assert body["outcome"]["error"] == f"Backend {BACKEND} is not responding (timeout)"
    assert body["service"]["status"] == "unhealthy"


def test_quick_services_show_site_block(setup):
    api, _, _ = setup
    call(api, "POST", "/api/services", {"name": "quick", "url": SITE})

    view = call(api, "GET", "/api/services/quick")[1]

    assert view["service"]["check_mode"] == "quick"
    assert view["site"]["check_mode"] == "quick"
    assert view["site"]["components"] == []


def test_invalid_check_mode(setup):
    api, _, _ = setup

    status, body = call(
        api, "POST", "/api/services", {"name": "x", "url": SITE, "check_mode": "deep"}
    )

    assert status == 400
    assert body["field"] == "check_mode"


def test_add_manual_backend_then_disable_enable_remove(setup):
    api, site, _ = setup
    add_full(api)
    manual = "https://extra-api.example-co.dev/health"

    status, body = call(
        api,
        "POST",
        "/api/services/expense/components",
        {"url": manual, "kind": "backend"},
    )
    assert status == 200
    [pending] = [c for c in body["site"]["components"] if c["url"] == manual]
    assert pending["status"] == "pending"
    assert pending["source"] == "manual"

    call(api, "POST", "/api/services/expense/check")
    assert manual in site.urls

    status, body = call(
        api, "POST", "/api/services/expense/components/disable", {"url": manual}
    )
    assert status == 200
    site.urls.clear()
    call(api, "POST", "/api/services/expense/check")
    assert manual not in site.urls
    view = call(api, "GET", "/api/services/expense")[1]["site"]
    assert {c["url"]: c["status"] for c in view["components"]}[manual] == "disabled"

    assert (
        call(api, "POST", "/api/services/expense/components/enable", {"url": manual})[0]
        == 200
    )
    assert (
        call(api, "POST", "/api/services/expense/components/remove", {"url": manual})[0]
        == 200
    )
    view = call(api, "GET", "/api/services/expense")[1]["site"]
    assert manual not in {c["url"] for c in view["components"]}


def test_disable_discovered_backend(setup):
    api, site, _ = setup
    add_full(api)
    call(api, "POST", "/api/services/expense/check")
    site.backend_up = False

    call(api, "POST", "/api/services/expense/components/disable", {"url": BACKEND})
    body = call(api, "POST", "/api/services/expense/check")[1]

    assert body["outcome"]["is_healthy"] is True
    status, body = call(
        api, "POST", "/api/services/expense/components/remove", {"url": BACKEND}
    )
    assert status == 400
    assert "turn discovered ones off" in body["message"]


@pytest.mark.parametrize(
    ("body", "status", "field"),
    [
        ({"url": "http://localhost:9000/api"}, 400, "url"),
        ({"url": "http://192.168.1.20/"}, 400, "url"),
        ({"url": "ftp://files.example.com"}, 400, "url"),
        ({"url": ""}, 400, "url"),
        ({"url": "https://ok.example-co.dev", "kind": "database"}, 400, "kind"),
    ],
)
def test_add_component_validation(setup, body, status, field):
    api, _, _ = setup
    add_full(api)

    code, payload = call(api, "POST", "/api/services/expense/components", body)

    assert code == status
    assert payload["field"] == field


def test_add_component_rejects_dns_to_private(setup, fake_dns):
    api, _, _ = setup
    add_full(api)
    fake_dns.mapping["sneaky.example-co.dev"] = ["127.0.0.1"]

    code, payload = call(
        api,
        "POST",
        "/api/services/expense/components",
        {"url": "https://sneaky.example-co.dev/"},
    )

    assert code == 400
    assert "127.0.0.1" in payload["message"]


def test_component_duplicates_and_limit(setup):
    api, _, _ = setup
    add_full(api)
    path = "/api/services/expense/components"

    assert call(api, "POST", path, {"url": "https://a.example-co.dev/"})[0] == 200
    dup = call(api, "POST", path, {"url": "https://a.example-co.dev/"})
    assert dup[0] == 400 and "already listed" in dup[1]["message"]
    for i in range(9):
        assert (
            call(api, "POST", path, {"url": f"https://s{i}.example-co.dev/"})[0] == 200
        )
    over = call(api, "POST", path, {"url": "https://one-more.example-co.dev/"})
    assert over[0] == 400 and "at most 10" in over[1]["message"]


def test_component_errors(setup):
    api, _, _ = setup
    add_full(api)
    call(api, "POST", "/api/services/expense/check")

    assert (
        call(
            api,
            "POST",
            "/api/services/expense/components/disable",
            {"url": "https://unknown.example-co.dev/"},
        )[0]
        == 404
    )
    status, body = call(
        api, "POST", "/api/services/expense/components/disable", {"url": SITE}
    )
    assert status == 400 and "always checked" in body["message"]
    assert (
        call(api, "POST", "/api/services/expense/components/explode", {"url": SITE})[0]
        == 404
    )
    assert call(api, "POST", "/api/services/nope/components", {"url": SITE})[0] == 404
    assert call(api, "GET", "/api/services/expense/components")[0] == 405


def test_component_changes_require_permission(setup):
    api, _, _ = setup
    add_full(api)

    status, _ = call(
        api,
        "POST",
        "/api/services/expense/components",
        {"url": "https://a.example-co.dev/"},
        context=RequestContext(can_write=False),
    )

    assert status == 403


def test_history_api_exposes_component_counts(setup):
    api, _, _ = setup
    add_full(api)
    call(api, "POST", "/api/services/expense/check")

    [check] = call(api, "GET", "/api/services/expense/checks")[1]["items"]

    assert check["check_mode"] == "full"
    assert check["component_counts"] == {"up": 3}
    assert json.dumps(check)  # serializable


def test_scripted_checker_services_still_work(fake_dns, clock):
    # Regression guard: quick services keep using the plain checker.
    config = load_config({})
    registry = ServiceRegistry(config, InMemoryServiceStore(clock))
    deps = Dependencies(
        history=HealthCheckHistory(),
        incidents=InMemoryIncidentStorage(),
        notifier=RecordingNotifier(),
        checker=ScriptedChecker(clock, True),  # type: ignore[arg-type]
        clock=clock,
    )
    api = DashboardApi(
        config,
        deps.history,
        deps.incidents,
        clock,
        registry=registry,
        runner=MonitorRunner(registry, deps, config),
    )
    call(api, "POST", "/api/services", {"name": "q", "url": SITE})

    assert call(api, "POST", "/api/services/q/check")[1]["outcome"]["is_healthy"]
