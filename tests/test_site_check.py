"""Full-site checks: discovery, probing, aggregation, limits and persistence.

No real network: a scripted ``FakeSite`` transport answers every request.
"""

import io
import json
from datetime import timedelta
from urllib.error import HTTPError, URLError

import pytest

from cloudpulse.config import ServiceConfig, load_config
from cloudpulse.dynamodb_incident_storage import DynamoDBIncidentStorage
from cloudpulse.factory import Dependencies, build_monitor
from cloudpulse.health_check import HttpHealthChecker
from cloudpulse.history import HealthCheckHistory
from cloudpulse.incident_manager import Transition
from cloudpulse.incident_storage import InMemoryIncidentStorage, JsonIncidentStorage
from cloudpulse.notifications import RecordingNotifier
from cloudpulse.site_check import (
    PRIVATE_BACKEND,
    FullSiteChecker,
    SiteLimits,
    analyze_html,
    describe_failure,
    fetch,
    find_backend_origins,
    load_profile,
)
from cloudpulse.url_safety import UrlPolicy
from conftest import HEALTH_TABLE, INCIDENTS_TABLE, STATE_TABLE

SITE = "https://app.example-site.com/"
BACKEND = "https://expense-api.onrender.com/"

# Shaped like a Vite/React SPA build.
SPA_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="UTF-8" />
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://api.partner-service.io">
<link rel="stylesheet" href="/assets/index-a1b2.css">
<script type="module" crossorigin src="/assets/index-a1b2.js"></script>
<link rel="modulepreload" href="/assets/vendor-c3d4.js">
<script src="https://www.googletagmanager.com/gtag/js?id=G-1"></script>
</head><body><div id="root"></div>
<a href="/about">About</a> <a href="/pricing#plans">Pricing</a>
<a href="https://twitter.com/someone">Twitter</a>
<a href="mailto:me@example.com">Mail</a>
<a href="/logo.png">Logo</a> <a href="#top">Top</a> <a href="/">Home</a>
</body></html>"""

BUNDLE = (
    'var w="http://www.w3.org/2000/svg";'
    'const api=axios.create({baseURL:"https://expense-api.onrender.com/api",'
    'headers:{"Content-Type":"application/json"}});'
    'fetch("https://fonts.googleapis.com/css2?family=Inter");'
    'const docs="https://react.dev/link/invalid-hook-call";'
    'const sentry="https://o1.ingest.sentry.io/123";'
    'const img="https://images.some-cdn.net/hero.png";'
    'const firebase="https://identitytoolkit.googleapis.com/v1";'
)


class Response:
    def __init__(self, status, headers, body, site):
        self.status = status
        self.headers = headers
        self._body = body
        self._site = site

    def read(self, n=-1):
        self._site.reads.append(n)
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSite:
    """Scripted transport: url -> (status, headers, body) | exception."""

    def __init__(self, routes=None, clock_step=0.0, monotonic=None):
        self.routes = dict(routes or {})
        self.requests = []
        self.reads = []
        self.clock_step = clock_step
        self.monotonic = monotonic

    def __call__(self, request, timeout, **_):
        self.requests.append(request)
        if self.monotonic is not None:
            self.monotonic.advance(self.clock_step)
        route = self.routes.get(request.full_url, (404, {}, b"not found"))
        if isinstance(route, BaseException):
            raise route
        status, headers, body = route
        if status >= 300:
            raise HTTPError(request.full_url, status, "x", headers, io.BytesIO(body))
        return Response(status, headers, body, self)

    def urls(self):
        return [r.full_url for r in self.requests]


class Mono:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


HTML = {"Content-Type": "text/html; charset=utf-8"}
JS = {"Content-Type": "application/javascript"}


def healthy_routes(**overrides):
    routes = {
        SITE: (200, HTML, SPA_HTML.encode()),
        "https://app.example-site.com/assets/index-a1b2.css": (200, {}, b"body{}"),
        "https://app.example-site.com/assets/index-a1b2.js": (200, JS, BUNDLE.encode()),
        "https://app.example-site.com/assets/vendor-c3d4.js": (200, JS, b"export{}"),
        "https://app.example-site.com/about": (200, HTML, b"<html>about</html>"),
        "https://app.example-site.com/pricing": (200, HTML, b"<html>pricing</html>"),
        BACKEND: (404, {}, b"Cannot GET /"),  # server answered: up
        "https://api.partner-service.io/": (200, {}, b"ok"),
    }
    routes.update(overrides)
    return routes


def checker(site, storage=None, clock=None, **kwargs):
    return FullSiteChecker(
        storage=storage if storage is not None else InMemoryIncidentStorage(),
        policy=UrlPolicy(),
        transport=site,
        **({"clock": clock} if clock else {}),
        **kwargs,
    )


def by_url(result_components):
    return {c["url"]: c for c in result_components}


# -- discovery ---------------------------------------------------------------------


def test_analyze_html_finds_own_assets_pages_and_preconnect():
    info = analyze_html(SPA_HTML, SITE)

    assert info.scripts == [
        "https://app.example-site.com/assets/index-a1b2.js",
        "https://app.example-site.com/assets/vendor-c3d4.js",
    ]
    assert info.styles == ["https://app.example-site.com/assets/index-a1b2.css"]
    assert info.pages == [
        "https://app.example-site.com/about",
        "https://app.example-site.com/pricing",
    ]
    # fonts.googleapis.com is noise; the API preconnect is kept as an origin.
    assert info.preconnect_origins == ["https://api.partner-service.io/"]


def test_analyze_html_respects_caps_and_base_href():
    links = "".join(f'<a href="p{i}">x</a>' for i in range(30))
    html = f'<base href="/docs/">{links}'
    info = analyze_html(html, SITE, SiteLimits(max_pages=3))

    assert info.pages == [f"https://app.example-site.com/docs/p{i}" for i in range(3)]


def test_analyze_html_survives_garbage():
    info = analyze_html("<html><script src=x.js><<<<</ht", SITE)

    assert info.scripts == ["https://app.example-site.com/x.js"]


def test_bundle_scan_finds_backend_and_filters_noise():
    found = dict(find_backend_origins(BUNDLE, SITE))

    assert found == {BACKEND: 3}


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        (
            'VITE_API_URL:"https://my-app-backend.fly.dev"',
            ["https://my-app-backend.fly.dev/"],
        ),
        (
            'const API_URL="https://svc.mycompany.dev/v1"',
            ["https://svc.mycompany.dev/"],
        ),
        ('fetch("https://data.mycompany.dev/items")', ["https://data.mycompany.dev/"]),
        (
            'REACT_APP_SERVER="https://srv.example-co.dev"',
            ["https://srv.example-co.dev/"],
        ),
        ('"https://unrelated-blog.dev/post"', []),
        ('a.href="https://github.com/org/repo"', []),
        ('baseURL:"http://localhost:5000/api"', ["http://localhost:5000/"]),
        ('url:"https://api.stripe.com/v1"', []),
    ],
)
def test_backend_patterns(snippet, expected):
    assert [o for o, _ in find_backend_origins(snippet, SITE)] == expected


def test_own_origin_is_not_a_backend():
    assert (
        find_backend_origins('fetch("https://app.example-site.com/api/x")', SITE) == []
    )


# -- checking -------------------------------------------------------------


def test_healthy_full_check(clock):
    site = FakeSite(healthy_routes())
    storage = InMemoryIncidentStorage()

    result = checker(site, storage, clock).check(SITE, service="app")

    assert result["is_healthy"] is True
    assert result["check_mode"] == "full"
    assert result["status_code"] == 200
    assert result["error"] is None
    assert result["component_counts"] == {"up": 8}
    components = by_url(load_profile(storage, "app").last_components)
    assert components[SITE]["role"] == "main"
    assert components[BACKEND] == {
        **components[BACKEND],
        "kind": "backend",
        "source": "discovered",
        "status": "up",
        "status_code": 404,
    }
    assert components["https://api.partner-service.io/"]["status"] == "up"
    assert components["https://app.example-site.com/about"]["kind"] == "page"
    # third-party URLs are never contacted
    assert not any("googleapis" in u or "react.dev" in u for u in site.urls())


def test_requests_are_polite_get_only_without_credentials(clock):
    site = FakeSite(healthy_routes())

    checker(site, clock=clock).check(SITE, service="app")

    for request in site.requests:
        assert request.get_method() == "GET"
        assert request.data is None
        assert request.get_header("User-agent").startswith("CloudPulse/")
        assert request.get_header("Cookie") is None
        assert request.get_header("Authorization") is None
    # the backend is probed at its origin root, never at an API action path
    assert BACKEND in site.urls()
    assert "https://expense-api.onrender.com/api" not in site.urls()


@pytest.mark.parametrize("status", [401, 403, 404, 405])
def test_backend_answering_with_client_error_is_up(clock, status):
    site = FakeSite(healthy_routes(**{BACKEND: (status, {}, b"")}))

    assert checker(site, clock=clock).check(SITE, service="app")["is_healthy"] is True


def test_frontend_ok_but_backend_timeout_is_unhealthy(clock):
    site = FakeSite(healthy_routes(**{BACKEND: URLError(TimeoutError("timed out"))}))

    result = checker(site, clock=clock).check(SITE, service="app")

    assert result["is_healthy"] is False
    assert result["status_code"] == 200  # the page itself is fine
    assert result["error"] == f"Backend {BACKEND} is not responding (timeout)"
    assert result["error_type"] == "TIMEOUT"
    assert result["failed_components"] == [
        {
            "url": BACKEND,
            "kind": "backend",
            "status_code": None,
            "error_type": "TIMEOUT",
        }
    ]


def test_backend_server_error_is_down(clock):
    site = FakeSite(healthy_routes(**{BACKEND: (503, {}, b"")}))

    result = checker(site, clock=clock).check(SITE, service="app")

    assert result["error"] == f"Backend {BACKEND} returned a server error (HTTP 503)"


def test_missing_asset_and_broken_page_are_failures(clock):
    site = FakeSite(
        healthy_routes(
            **{
                "https://app.example-site.com/assets/index-a1b2.css": (404, {}, b""),
                "https://app.example-site.com/about": (500, {}, b""),
            }
        )
    )

    result = checker(site, clock=clock).check(SITE, service="app")

    assert result["is_healthy"] is False
    assert result["error"].startswith(
        "File https://app.example-site.com/assets/index-a1b2.css is missing (HTTP 404)"
    )
    assert result["error"].endswith("(and 1 more problem)")


def test_main_page_failure_still_reports_known_backends(clock):
    storage = InMemoryIncidentStorage()
    checker(FakeSite(healthy_routes()), storage, clock).check(SITE, service="app")

    site = FakeSite(healthy_routes(**{SITE: (502, {}, b"")}))
    result = checker(site, storage, clock).check(SITE, service="app")

    assert result["is_healthy"] is False
    assert result["error"] == f"The main page {SITE} returned a server error (HTTP 502)"
    assert BACKEND in site.urls()


def test_discovered_private_backend_is_never_contacted_and_counts_as_down(clock):
    bundle = 'axios.create({baseURL:"http://localhost:5000/api"})'
    site = FakeSite(
        healthy_routes(
            **{
                "https://app.example-site.com/assets/index-a1b2.js": (
                    200,
                    JS,
                    bundle.encode(),
                )
            }
        )
    )

    result = checker(site, clock=clock).check(SITE, service="app")

    assert not any("localhost" in u for u in site.urls())
    assert result["is_healthy"] is False
    assert result["error_type"] == PRIVATE_BACKEND
    assert "http://localhost:5000/" in result["error"]
    assert "visitors' browsers cannot reach it" in result["error"]


def test_discovered_hostname_resolving_privately_is_blocked_before_connecting(fake_dns):
    fake_dns.mapping["internal-api.example-site.com"] = ["10.0.0.8"]

    result = fetch(
        "https://internal-api.example-site.com/",
        timeout=5,
        read_limit=0,
        policy=UrlPolicy(),
        transport=None,  # real guarded transport: DNS check happens first
        max_redirects=5,
    )

    assert result.error_type == "BLOCKED_DESTINATION"
    assert "10.0.0.8" in result.error


def test_redirect_to_metadata_is_blocked(clock):
    site = FakeSite(
        healthy_routes(
            **{BACKEND: (302, {"Location": "http://169.254.169.254/latest"}, b"")}
        )
    )

    result = checker(site, clock=clock).check(SITE, service="app")

    assert "169.254.169.254" not in " ".join(site.urls())
    assert result["error_type"] == PRIVATE_BACKEND


def test_caps_on_pages_assets_backends(clock):
    many_links = "".join(f'<a href="/p{i}">p</a>' for i in range(20))
    many_scripts = "".join(f'<script src="/s{i}.js"></script>' for i in range(20))
    backends = ";".join(f'fetch("https://svc{i}.onrender.com/x")' for i in range(9))
    routes = {SITE: (200, HTML, (many_links + many_scripts).encode())}
    routes.update(
        {f"https://app.example-site.com/p{i}": (200, HTML, b"") for i in range(20)}
    )
    routes.update(
        {f"https://app.example-site.com/s{i}.js": (200, JS, b"") for i in range(20)}
    )
    routes["https://app.example-site.com/s0.js"] = (200, JS, backends.encode())
    routes.update({f"https://svc{i}.onrender.com/": (200, {}, b"") for i in range(9)})
    site = FakeSite(routes)
    limits = SiteLimits(max_pages=3, max_assets=4, max_backends=2)

    checker(site, clock=clock, limits=limits).check(SITE, service="app")

    urls = site.urls()
    assert sum("/p" in u for u in urls) == 3
    assert sum(u.endswith(".js") for u in urls) == 4
    assert sum("onrender.com" in u for u in urls) == 2


def test_byte_budget_limits_downloads(clock):
    big = ("x" * 10_000 + 'fetch("https://late-api.onrender.com/x")').encode()
    site = FakeSite(
        healthy_routes(
            **{"https://app.example-site.com/assets/index-a1b2.js": (200, JS, big)}
        )
    )
    limits = SiteLimits(max_total_bytes=len(SPA_HTML) + 5_000)

    checker(site, clock=clock, limits=limits).check(SITE, service="app")

    # The bundle was truncated before the late URL, so it was never discovered.
    assert "https://late-api.onrender.com/" not in site.urls()
    assert max(n for n in site.reads if n > 0) <= limits.max_html_bytes


def test_time_budget_skips_remaining_components_without_failing(clock):
    mono = Mono()
    site = FakeSite(healthy_routes(), clock_step=40, monotonic=mono)
    limits = SiteLimits(budget_seconds=90)

    result = checker(site, clock=clock, limits=limits, monotonic=mono).check(
        SITE, service="app"
    )

    assert result["component_counts"].get("skipped", 0) > 0
    assert result["is_healthy"] is True
    assert len(site.requests) <= 3


def test_non_html_main_url_checks_only_itself_and_known_backends(clock):
    site = FakeSite(
        {
            "https://api.example-site.com/health": (
                200,
                {"Content-Type": "application/json"},
                b"{}",
            )
        }
    )

    result = checker(site, clock=clock).check(
        "https://api.example-site.com/health", service="api"
    )

    assert result["is_healthy"] is True
    assert result["component_counts"] == {"up": 1}


# -- discovery cache, overrides and persistence ------------------------------------


@pytest.fixture(params=["memory", "json", "dynamodb"])
def storage(request, tmp_path):
    if request.param == "memory":
        return InMemoryIncidentStorage()
    if request.param == "json":
        return JsonIncidentStorage(tmp_path / "incidents.json")
    resource = request.getfixturevalue("dynamodb")
    return DynamoDBIncidentStorage(
        INCIDENTS_TABLE,
        table=resource.Table(INCIDENTS_TABLE),
        state_table=resource.Table(STATE_TABLE),
    )


def test_profile_is_persisted_and_reused(storage, clock):
    checker(FakeSite(healthy_routes()), storage, clock).check(SITE, service="app")

    profile = load_profile(storage, "app")
    assert set(profile.backends) == {"https://api.partner-service.io/", BACKEND}
    assert profile.discovered_at == clock()
    assert profile.last_checked_at == clock()
    assert storage.count() == 0  # profiles are not incidents

    # Within 6 hours and with the same bundle: no bundle download.
    clock.advance(hours=1)
    site = FakeSite(healthy_routes())
    checker(site, storage, clock).check(SITE, service="app")
    assert all(n <= 0 for n in site.reads[1:])  # only the HTML body was read
    assert BACKEND in site.urls()


def test_rediscovery_on_new_bundle_staleness_or_force(storage, clock):
    checker(FakeSite(healthy_routes()), storage, clock).check(SITE, service="app")

    # A deploy changes the hashed bundle name: rediscover immediately.
    new_html = SPA_HTML.replace("index-a1b2.js", "index-zz99.js")
    routes = healthy_routes(**{SITE: (200, HTML, new_html.encode())})
    routes["https://app.example-site.com/assets/index-zz99.js"] = (
        200,
        JS,
        b'fetch("https://new-backend.onrender.com/x")',
    )
    routes["https://new-backend.onrender.com/"] = (200, {}, b"")
    checker(FakeSite(routes), storage, clock).check(SITE, service="app")
    assert "https://new-backend.onrender.com/" in load_profile(storage, "app").backends

    # Stale after 6 hours.
    clock.advance(hours=7)
    site = FakeSite(routes)
    checker(site, storage, clock).check(SITE, service="app")
    assert any(n > 0 for n in site.reads[1:])

    # Forced ("Check now").
    clock.advance(minutes=1)
    site = FakeSite(routes)
    checker(site, storage, clock, force_discover=True).check(SITE, service="app")
    assert any(n > 0 for n in site.reads[1:])


def test_disabled_and_manual_components(storage, clock):
    checker(FakeSite(healthy_routes()), storage, clock).check(SITE, service="app")
    profile = load_profile(storage, "app").to_dict()
    profile["disabled"] = [BACKEND]
    profile["manual"] = [
        {"url": "https://extra-api.example-co.dev/health", "kind": "backend"}
    ]
    storage.save_site_profile("app", profile)

    routes = healthy_routes(**{BACKEND: (503, {}, b"")})  # noisy, but turned off
    routes["https://extra-api.example-co.dev/health"] = (500, {}, b"")
    site = FakeSite(routes)
    result = checker(site, storage, clock).check(SITE, service="app")

    assert BACKEND not in site.urls()
    assert result["error"] == (
        "Backend https://extra-api.example-co.dev/health returned a server "
        "error (HTTP 500)"
    )
    statuses = {
        c["url"]: c["status"] for c in load_profile(storage, "app").last_components
    }
    assert statuses[BACKEND] == "disabled"
    # user settings survive the checker's save
    saved = load_profile(storage, "app")
    assert saved.disabled == [BACKEND]
    assert saved.manual[0]["url"] == "https://extra-api.example-co.dev/health"


def test_describe_failure_wording():
    assert (
        describe_failure(
            {
                "url": "https://x.onrender.com/",
                "kind": "backend",
                "error_type": "DNS_ERROR",
            }
        )
        == "Backend https://x.onrender.com/ cannot be found (DNS lookup failed)"
    )
    assert (
        describe_failure(
            {
                "url": "https://a/p",
                "kind": "page",
                "status_code": 403,
                "error_type": "HTTP_STATUS",
            }
        )
        == "Page https://a/p returned HTTP 403"
    )


# -- integration with monitoring / incidents ------------------------------------------


def test_full_mode_service_opens_incident_naming_the_backend(clock):
    site = FakeSite(healthy_routes(**{BACKEND: URLError(TimeoutError("timed out"))}))
    notifier = RecordingNotifier()
    deps = Dependencies(
        history=HealthCheckHistory(),
        incidents=InMemoryIncidentStorage(),
        notifier=notifier,
        checker=HttpHealthChecker(transport=site, clock=clock),
        clock=clock,
    )
    service = ServiceConfig(
        name="expense",
        url=SITE,
        failure_threshold=1,
        check_mode="full",
        backend_timeout_seconds=30,
    )

    outcome = build_monitor(service, deps, load_config({})).run()

    assert outcome.transition is Transition.INCIDENT_OPENED
    assert (
        outcome.to_dict()["error"] == f"Backend {BACKEND} is not responding (timeout)"
    )
    message = notifier.events[0].message
    assert f"Last error: Backend {BACKEND} is not responding (timeout)" in message
    record = deps.history.get_recent("expense", 1)[0]
    assert record["check_mode"] == "full"
    assert "components" not in record  # history stays compact
    assert record["failed_components"][0]["url"] == BACKEND


def test_quick_mode_is_unchanged(clock):
    site = FakeSite(healthy_routes(**{BACKEND: URLError(TimeoutError("t"))}))
    deps = Dependencies(
        history=HealthCheckHistory(),
        incidents=InMemoryIncidentStorage(),
        notifier=RecordingNotifier(),
        checker=HttpHealthChecker(transport=site, clock=clock),
        clock=clock,
    )

    outcome = build_monitor(
        ServiceConfig(name="q", url=SITE), deps, load_config({})
    ).run()

    assert outcome.result["is_healthy"] is True
    assert site.urls() == [SITE]


def test_service_config_round_trip_with_check_mode():
    config = load_config(
        {
            "CLOUDPULSE_SERVICES": json.dumps(
                [
                    {
                        "name": "a",
                        "url": SITE,
                        "check_mode": "full",
                        "backend_timeout_seconds": 45,
                    }
                ]
            )
        }
    )
    [service] = config.services

    assert service.check_mode == "full"
    assert service.backend_timeout_seconds == 45
    assert service.max_check_seconds > service.timeout_seconds
    assert service.to_storage_dict()["check_mode"] == "full"


@pytest.mark.parametrize(
    ("data", "field"),
    [
        ({"check_mode": "deep"}, "check_mode"),
        ({"backend_timeout_seconds": 0}, "backend_timeout_seconds"),
        ({"backend_timeout_seconds": 61}, "backend_timeout_seconds"),
    ],
)
def test_check_mode_validation(data, field):
    from cloudpulse.config import parse_service
    from cloudpulse.errors import ServiceValidationError

    with pytest.raises(ServiceValidationError) as exc:
        parse_service(
            {"name": "a", "url": SITE, **data},
            index=None,
            default_timeout=5,
            default_threshold=3,
            policy=UrlPolicy(),
        )
    assert exc.value.field == field


def test_dynamodb_history_accepts_full_results(dynamodb, clock):
    from cloudpulse.dynamodb_storage import DynamoDBHealthCheckStorage

    store = DynamoDBHealthCheckStorage(HEALTH_TABLE, table=dynamodb.Table(HEALTH_TABLE))
    result = checker(FakeSite(healthy_routes()), clock=clock).check(SITE, service="app")
    store.add(result)

    assert store.get_recent("app", 1)[0]["component_counts"] == {"up": 8}


def test_time_left_is_respected_by_runner(clock):
    service = ServiceConfig(name="a", url=SITE, check_mode="full")
    assert service.max_check_seconds >= 60
    assert timedelta(seconds=service.max_check_seconds)
