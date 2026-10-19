"""Full-site checks: monitor a whole web app from its main URL.

A static host (Firebase, Netlify, S3, ...) keeps answering ``200`` for the
page even when the backend API the app depends on is down. A *full* check
therefore looks at what the page actually needs:

1. **Main page** — fetched and judged like a quick check.
2. **Own assets** — same-site ``<script src>`` / stylesheets / module
   preloads from the *current* HTML (re-read on every check, so hashed bundle
   names after a deploy never cause false alarms).
3. **Same-site pages** — a bounded number of ``<a href>`` links.
4. **Backends** — origins the JavaScript talks to. Discovered by scanning the
   same-site JS bundles (bounded) for absolute URLs in API-like contexts
   (``fetch``/``axios``/``baseURL``/``API_URL``/``VITE_*``...) or on known
   hosting platforms, plus ``<link rel=preconnect>``. Third-party noise
   (fonts, CDNs, analytics, Google, w3.org, ...) is filtered and results are
   de-duplicated by origin. Each backend gets one **GET of its origin root**
   (never POST, no forms, no user data): any HTTP answer below 500 — including
   404/401/405 — means "up"; 5xx, DNS/TLS/connection errors and timeouts mean
   "down". The timeout is generous (cold starts) and configurable.

Discovery results are cached per service (a *site profile*) and refreshed
every 6 hours, when the bundle set changes, or on "Check now". Users can turn
off a noisy discovered component or add one manually.

Safety: every URL goes through :mod:`cloudpulse.url_safety` (scheme, DNS
resolution, connected peer, redirects). Private/loopback/metadata targets are
recorded as *blocked* and never contacted. Requests are sequential (never
concurrent against one site), carry a polite User-Agent, no cookies and no
credentials, and are bounded by page/asset/backend counts, bytes and time.

Limitations (also shown in the UI): features behind a login, failures of a
specific POST action while the server is up (e.g. an AI provider error), and
backends only discovered at runtime cannot be seen. A dedicated ``/health``
endpoint that exercises dependencies is the way to cover those.
"""

import hashlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request
from uuid import uuid4

from cloudpulse import health_check
from cloudpulse.clock import Clock, parse_utc_iso, to_utc_iso, utc_now
from cloudpulse.errors import StorageError
from cloudpulse.health_check import (
    DEFAULT_ACCEPTED_STATUS,
    DEFAULT_MAX_REDIRECTS,
    ErrorType,
    Transport,
    classify_exception,
)
from cloudpulse.url_safety import UrlPolicy, redact_url

logger = logging.getLogger(__name__)

FULL_CHECK_BUDGET_SECONDS = 90.0
SITE_USER_AGENT = "CloudPulse/0.2 (+site-check; read-only GET requests, no cookies)"
KINDS = ("page", "asset", "backend")
# A discovered backend URL that points to localhost/private space. Never
# contacted; on a public site this means visitors' browsers cannot reach the
# API (typically a development API URL baked into the production build).
PRIVATE_BACKEND = "PRIVATE_BACKEND"
MANUAL_KINDS = ("page", "backend")


@dataclass(frozen=True)
class SiteLimits:
    max_pages: int = 10
    max_assets: int = 30
    max_backends: int = 5
    max_manual: int = 10
    max_total_bytes: int = 5 * 1024 * 1024
    max_html_bytes: int = 2 * 1024 * 1024
    max_bundle_bytes: int = 2 * 1024 * 1024
    max_bundles: int = 10
    budget_seconds: float = FULL_CHECK_BUDGET_SECONDS
    rediscover_seconds: float = 6 * 3600


# -- third-party noise ------------------------------------------------------

NOISE_DOMAINS = (
    # standards, docs and examples
    "w3.org",
    "schema.org",
    "xmlns.com",
    "purl.org",
    "ogp.me",
    "json-schema.org",
    "example.com",
    "example.org",
    "example.net",
    "mozilla.org",
    "wikipedia.org",
    "creativecommons.org",
    "opensource.org",
    "apache.org",
    "gnu.org",
    "whatwg.org",
    "ietf.org",
    "unicode.org",
    # Google / Firebase SDK internals, fonts, analytics, ads
    "google.com",
    "googleapis.com",
    "gstatic.com",
    "googleusercontent.com",
    "google-analytics.com",
    "googletagmanager.com",
    "googlesyndication.com",
    "doubleclick.net",
    "gvt1.com",
    "youtube.com",
    "ytimg.com",
    "goo.gl",
    # social
    "facebook.com",
    "facebook.net",
    "fbcdn.net",
    "fb.me",
    "fb.com",
    "twitter.com",
    "x.com",
    "twimg.com",
    "linkedin.com",
    "instagram.com",
    "tiktok.com",
    # code hosting, CDNs, fonts, icons
    "github.com",
    "githubusercontent.com",
    "githubassets.com",
    "gitlab.com",
    "jsdelivr.net",
    "unpkg.com",
    "cdnjs.cloudflare.com",
    "cloudflareinsights.com",
    "bootstrapcdn.com",
    "fontawesome.com",
    "typekit.net",
    "fonts.net",
    "jquery.com",
    "gravatar.com",
    "wp.com",
    # framework / library documentation links embedded in bundles
    "reactjs.org",
    "react.dev",
    "vuejs.org",
    "angular.io",
    "svelte.dev",
    "vitejs.dev",
    "vite.dev",
    "nextjs.org",
    "nodejs.org",
    "npmjs.com",
    "npmjs.org",
    "reactrouter.com",
    "remix.run",
    "tanstack.com",
    "redux.js.org",
    "mui.com",
    "tailwindcss.com",
    "lodash.com",
    "momentjs.com",
    "chartjs.org",
    "d3js.org",
    "axios-http.com",
    "zod.dev",
    "babeljs.io",
    "webpack.js.org",
    "jestjs.io",
    "typescriptlang.org",
    "polyfill.io",
    "feross.org",
    # monitoring, analytics, payments, support widgets
    "sentry.io",
    "sentry-cdn.com",
    "segment.com",
    "segment.io",
    "mixpanel.com",
    "hotjar.com",
    "intercom.io",
    "intercomcdn.com",
    "stripe.com",
    "stripe.network",
    "paypal.com",
    "clarity.ms",
    "plausible.io",
    "posthog.com",
    "amplitude.com",
    "newrelic.com",
    "nr-data.net",
    "datadoghq.com",
    # big vendor sites
    "apple.com",
    "microsoft.com",
    "bing.com",
    "openstreetmap.org",
    "mapbox.com",
)

# Hosting platforms where a referenced origin is very likely an app backend.
BACKEND_PLATFORMS = (
    "onrender.com",
    "herokuapp.com",
    "railway.app",
    "fly.dev",
    "vercel.app",
    "netlify.app",
    "workers.dev",
    "amazonaws.com",
    "azurewebsites.net",
    "azurecontainerapps.io",
    "cloudfunctions.net",
    "run.app",
    "appspot.com",
    "supabase.co",
    "firebaseio.com",
    "ngrok.io",
    "ngrok-free.app",
    "glitch.me",
    "repl.co",
    "replit.app",
    "pythonanywhere.com",
    "koyeb.app",
    "deta.dev",
    "deno.dev",
    "cyclic.app",
    "adaptable.app",
    "hf.space",
)

_API_CONTEXT = re.compile(
    r"(api|backend|server|base_?url|baseurl|endpoint|graphql|socket|fetch\s*\(|"
    r"axios|VITE_|REACT_APP_|NEXT_PUBLIC_|NUXT_|EXPO_PUBLIC_)",
    re.IGNORECASE,
)
_ABSOLUTE_URL = re.compile(
    r"https?://[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::\d{2,5})?"
    r"(?=[/\"'`\s?#),;\\]|$)"
)
_SKIP_PAGE_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".css",
    ".js",
    ".mjs",
    ".json",
    ".xml",
    ".txt",
    ".mp4",
    ".mp3",
    ".woff",
    ".woff2",
    ".ttf",
    ".map",
)


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_noise_host(host: str) -> bool:
    return _host_matches(host.lower(), NOISE_DOMAINS)


def origin_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default = 443 if parts.scheme == "https" else 80
    netloc = host if port in (None, default) else f"{host}:{port}"
    return urlunsplit((parts.scheme, netloc, "/", "", ""))


def same_site(url: str, base: str) -> bool:
    return origin_of(url) is not None and origin_of(url) == origin_of(base)


# -- HTML analysis ------------------------------------------------------------


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.styles: list[str] = []
        self.preconnect: list[str] = []
        self.links: list[str] = []
        self.inline_js: list[str] = []
        self.base_href: str | None = None
        self._in_inline_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "base" and a.get("href") and self.base_href is None:
            self.base_href = a["href"]
        elif tag == "script":
            if a.get("src"):
                self.scripts.append(a["src"])
            else:
                self._in_inline_script = True
        elif tag == "link":
            rel = set(a.get("rel", "").lower().split())
            href = a.get("href")
            if not href:
                return
            if "stylesheet" in rel:
                self.styles.append(href)
            elif "modulepreload" in rel or (
                "preload" in rel and a.get("as") == "script"
            ):
                self.scripts.append(href)
            elif rel & {"preconnect", "dns-prefetch"}:
                self.preconnect.append(href)
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_inline_script = False

    def handle_data(self, data: str) -> None:
        if self._in_inline_script:
            self.inline_js.append(data)


@dataclass
class PageInfo:
    scripts: list[str] = field(default_factory=list)  # same-site JS
    styles: list[str] = field(default_factory=list)  # same-site CSS
    pages: list[str] = field(default_factory=list)  # same-site links
    preconnect_origins: list[str] = field(default_factory=list)
    inline_js: str = ""

    @property
    def assets(self) -> list[str]:
        return self.scripts + self.styles


def _clean(url: str, base: str) -> str | None:
    url = url.strip()
    if not url or url.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    absolute = urljoin(base, url)
    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https"):
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def analyze_html(
    html: str, base_url: str, limits: SiteLimits | None = None
) -> PageInfo:
    limits = limits or SiteLimits()
    parser = _PageParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup: keep what was collected
        logger.debug("HTML parsing stopped early for %s", redact_url(base_url))
    base = urljoin(base_url, parser.base_href) if parser.base_href else base_url

    def resolve(values: list[str]) -> list[str]:
        return _dedupe([u for u in (_clean(v, base) for v in values) if u])

    scripts = [u for u in resolve(parser.scripts) if same_site(u, base_url)]
    styles = [u for u in resolve(parser.styles) if same_site(u, base_url)]
    main = _clean(base_url, base_url)
    pages = []
    for url in resolve(parser.links):
        path = urlsplit(url).path.lower()
        if (
            same_site(url, base_url)
            and url != main
            and not path.endswith(_SKIP_PAGE_EXTENSIONS)
        ):
            pages.append(url)
    preconnect = []
    for url in resolve(parser.preconnect):
        origin = origin_of(url)
        host = urlsplit(url).hostname or ""
        if origin and not same_site(url, base_url) and not is_noise_host(host):
            preconnect.append(origin)
    return PageInfo(
        scripts=scripts[: limits.max_assets],
        styles=styles[: max(0, limits.max_assets - len(scripts))],
        pages=pages[: limits.max_pages],
        preconnect_origins=_dedupe(preconnect),
        inline_js="\n".join(parser.inline_js),
    )


def find_backend_origins(text: str, site_url: str) -> list[tuple[str, int]]:
    """Return ``(origin, score)`` for likely backend origins in JS text.

    Score 3: on a known backend hosting platform or an ``api``-named host;
    score 2: appears in an API-like context (fetch/axios/baseURL/API_URL/VITE_*).
    Plain URLs without such signals (documentation links, licences) are
    ignored to keep noise down.
    """
    scores: dict[str, int] = {}
    for match in _ABSOLUTE_URL.finditer(text):
        origin = origin_of(match.group(0))
        if origin is None or origin == origin_of(site_url):
            continue
        host = urlsplit(origin).hostname or ""
        if is_noise_host(host):
            continue
        if "." not in host and host != "localhost" and not host.startswith("["):
            continue
        score = 0
        if _host_matches(host, BACKEND_PLATFORMS) or re.search(
            r"(^|[.-])api([.-]|$)|backend|server", host
        ):
            score = 3
        else:
            context = text[max(0, match.start() - 80) : match.start()]
            path = text[match.end() : match.end() + 8]
            if _API_CONTEXT.search(context) or path.startswith("/api"):
                score = 2
        if score:
            scores[origin] = max(scores.get(origin, 0), score)
    return sorted(scores.items(), key=lambda item: -item[1])


# -- fetching ------------------------------------------------------------------


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None = None
    content_type: str = ""
    body: bytes = b""
    elapsed_ms: float = 0.0
    redirects: int = 0
    error_type: str | None = None
    error: str | None = None


class _Budget:
    def __init__(self, limits: SiteLimits, monotonic: Callable[[], float]) -> None:
        self.monotonic = monotonic
        self.started = monotonic()
        self.deadline = self.started + limits.budget_seconds
        self.bytes_left = limits.max_total_bytes

    def remaining(self) -> float:
        return self.deadline - self.monotonic()


def fetch(
    url: str,
    *,
    timeout: float,
    read_limit: int,
    policy: UrlPolicy,
    transport: Transport | None,
    max_redirects: int,
    monotonic: Callable[[], float] = time.perf_counter,
) -> FetchResult:
    """GET ``url`` through the SSRF-guarded transport, following redirects."""
    started = monotonic()
    result = FetchResult(url=url, final_url=url)
    current = url
    try:
        while True:
            policy.check_url(current)
            remaining = timeout - (monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("timed out")
            request = Request(
                current,
                method="GET",
                headers={
                    "User-Agent": SITE_USER_AGENT,
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                },
            )
            location = None
            try:
                if transport is not None:
                    response_cm = transport(request, timeout=remaining)
                else:
                    response_cm = health_check.urlopen(
                        request, timeout=remaining, policy=policy
                    )
                with response_cm as response:
                    result.status = int(response.status)
                    headers = getattr(response, "headers", None)
                    result.content_type = (
                        headers.get("Content-Type", "") if headers is not None else ""
                    ) or ""
                    if read_limit > 0:
                        result.body = response.read(read_limit) or b""
            except HTTPError as exc:
                result.status = exc.code
                location = exc.headers.get("Location") if exc.headers else None
                exc.close()
            if result.status is not None and 300 <= result.status < 400 and location:
                if result.redirects >= max_redirects:
                    result.error_type = ErrorType.REDIRECT_LIMIT.value
                    result.error = f"More than {max_redirects} redirects."
                    break
                result.redirects += 1
                current = urljoin(current, location)
                result.final_url = current
                continue
            break
    except Exception as exc:  # noqa: BLE001 - every failure becomes a result
        error_type, message = classify_exception(exc)
        result.status = None
        result.error_type = error_type.value
        result.error = message
    result.elapsed_ms = round(max(0.0, monotonic() - started) * 1000, 2)
    return result


# -- site profile ------------------------------------------------------------------


@dataclass
class SiteProfile:
    """Per-service discovery cache and user overrides (persisted)."""

    service: str
    discovered_at: datetime | None = None
    fingerprint: str | None = None
    backends: list[str] = field(default_factory=list)  # discovered origins
    manual: list[dict[str, str]] = field(default_factory=list)  # {"url", "kind"}
    disabled: list[str] = field(default_factory=list)  # urls turned off by the user
    last_components: list[dict[str, Any]] = field(default_factory=list)
    last_checked_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "discovered_at": to_utc_iso(self.discovered_at)
            if self.discovered_at
            else None,
            "fingerprint": self.fingerprint,
            "backends": list(self.backends),
            "manual": [dict(m) for m in self.manual],
            "disabled": list(self.disabled),
            "last_components": [dict(c) for c in self.last_components],
            "last_checked_at": (
                to_utc_iso(self.last_checked_at) if self.last_checked_at else None
            ),
        }

    @classmethod
    def from_dict(cls, service: str, data: dict[str, Any] | None) -> "SiteProfile":
        if not isinstance(data, dict):
            return cls(service=service)

        def when(key: str) -> datetime | None:
            try:
                return parse_utc_iso(data[key]) if data.get(key) else None
            except (TypeError, ValueError):
                return None

        def str_list(key: str) -> list[str]:
            value = data.get(key) or []
            return (
                [v for v in value if isinstance(v, str)]
                if isinstance(value, list)
                else []
            )

        manual = [
            {"url": m["url"], "kind": m.get("kind", "backend")}
            for m in (data.get("manual") or [])
            if isinstance(m, dict)
            and isinstance(m.get("url"), str)
            and m.get("kind", "backend") in MANUAL_KINDS
        ]
        components = [
            c for c in (data.get("last_components") or []) if isinstance(c, dict)
        ]
        return cls(
            service=service,
            discovered_at=when("discovered_at"),
            fingerprint=data.get("fingerprint")
            if isinstance(data.get("fingerprint"), str)
            else None,
            backends=str_list("backends"),
            manual=manual,
            disabled=str_list("disabled"),
            last_components=components,
            last_checked_at=when("last_checked_at"),
        )


def load_profile(storage: Any, service: str) -> SiteProfile:
    try:
        return SiteProfile.from_dict(service, storage.get_site_profile(service))
    except StorageError:
        logger.warning("Could not load site profile for %s", service)
        return SiteProfile(service=service)


# -- describing results -------------------------------------------------------------

_KIND_LABEL = {
    "page": "Page",
    "asset": "File",
    "backend": "Backend",
    "main": "The main page",
}


def describe_failure(component: dict[str, Any]) -> str:
    """Plain-language sentence naming the failing component."""
    label = _KIND_LABEL.get(component.get("role") or component["kind"], "Component")
    url = component["url"]
    error_type = component.get("error_type")
    code = component.get("status_code")
    if error_type == PRIVATE_BACKEND:
        problem = (
            "points to a local/private address, so visitors' browsers cannot "
            "reach it (the site was probably built with a development API URL)"
        )
    elif error_type == ErrorType.TIMEOUT:
        problem = "is not responding (timeout)"
    elif error_type == ErrorType.DNS_ERROR:
        problem = "cannot be found (DNS lookup failed)"
    elif error_type == ErrorType.CONNECTION_ERROR:
        problem = "refused or dropped the connection"
    elif error_type == ErrorType.TLS_ERROR:
        problem = "has a TLS/certificate problem"
    elif error_type == ErrorType.REDIRECT_LIMIT:
        problem = "redirects too many times"
    elif error_type == ErrorType.PROTOCOL_ERROR:
        problem = "sent an invalid HTTP response"
    elif code is not None and code >= 500:
        problem = f"returned a server error (HTTP {code})"
    elif code == 404:
        problem = "is missing (HTTP 404)"
    elif code is not None:
        problem = f"returned HTTP {code}"
    else:
        problem = f"failed ({component.get('error') or 'unknown error'})"
    return f"{label} {url} {problem}"


# -- the checker -------------------------------------------------------------------


@dataclass
class FullSiteChecker:
    """Checker with the same ``check()`` signature as ``HttpHealthChecker``."""

    storage: Any  # IncidentStorage with get/save_site_profile
    policy: UrlPolicy = field(default_factory=UrlPolicy)
    transport: Transport | None = None
    clock: Clock = utc_now
    limits: SiteLimits = field(default_factory=SiteLimits)
    backend_timeout: float = 20.0
    force_discover: bool = False
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    monotonic: Callable[[], float] = time.perf_counter

    def check(
        self,
        url: str,
        *,
        service: str | None = None,
        timeout: float = 5.0,
        accepted_status: tuple[int, int] = DEFAULT_ACCEPTED_STATUS,
    ) -> dict[str, Any]:
        name = service or url
        budget = _Budget(self.limits, self.monotonic)
        result: dict[str, Any] = {
            "id": str(uuid4()),
            "service": name,
            "url": redact_url(url),
            "checked_at": to_utc_iso(self.clock()),
            "check_mode": "full",
            "is_healthy": False,
            "status_code": None,
            "response_time_ms": 0.0,
            "error": None,
            "error_type": None,
            "redirects": 0,
        }
        low, high = accepted_status
        components: list[dict[str, Any]] = []

        def get(target: str, kind_timeout: float, read_limit: int = 0) -> FetchResult:
            limit = max(0, min(read_limit, budget.bytes_left))
            fetched = fetch(
                target,
                timeout=max(0.001, min(kind_timeout, budget.remaining())),
                read_limit=limit,
                policy=self.policy,
                transport=self.transport,
                max_redirects=self.max_redirects,
                monotonic=self.monotonic,
            )
            budget.bytes_left -= len(fetched.body)
            return fetched

        def record(
            target: str,
            kind: str,
            source: str,
            fetched: FetchResult | None,
            ok: bool | None,
            state: str | None = None,
        ) -> dict[str, Any]:
            component: dict[str, Any] = {
                "url": redact_url(target),
                "kind": kind,
                "source": source,
                "status": state or ("up" if ok else "down"),
                "status_code": fetched.status if fetched else None,
                "response_time_ms": fetched.elapsed_ms if fetched else None,
                "error_type": fetched.error_type if fetched else None,
                "error": fetched.error if fetched else None,
            }
            if source == "main":
                component["role"] = "main"
            components.append(component)
            return component

        # 1. main page
        main = get(url, timeout, self.limits.max_html_bytes)
        main_ok = main.status is not None and low <= main.status <= high
        if main.status is not None and not main_ok and main.error_type is None:
            main.error_type = ErrorType.HTTP_STATUS.value
            main.error = f"HTTP error: {main.status}"
        record(url, "page", "main", main, main_ok)
        result.update(
            status_code=main.status,
            response_time_ms=main.elapsed_ms,
            redirects=main.redirects,
        )
        if main.final_url != url:
            result["final_url"] = redact_url(main.final_url)

        profile = load_profile(self.storage, name)
        if main_ok:
            self._check_site(main, profile, components, get, record, budget, low, high)
        else:
            # Without the page we cannot see assets; still probe known backends
            # so the report says whether the API is up too.
            self._probe_backends(profile, components, get, record, budget)

        # aggregate (history keeps counts + failures; the full list is in the
        # site profile, so stored history stays small)
        failures = [c for c in components if c["status"] == "down"]
        counts: dict[str, int] = {}
        for component in components:
            counts[component["status"]] = counts.get(component["status"], 0) + 1
        result["component_counts"] = counts
        result["failed_components"] = [
            {k: c.get(k) for k in ("url", "kind", "status_code", "error_type")}
            for c in failures[:5]
        ]
        result["is_healthy"] = not failures
        if failures:
            first = failures[0]
            message = describe_failure(first)
            if len(failures) > 1:
                more = len(failures) - 1
                message += f" (and {more} more problem{'s' if more > 1 else ''})"
            result["error"] = message
            result["error_type"] = (
                first.get("error_type") or ErrorType.HTTP_STATUS.value
            )
            result["failed_component"] = {"url": first["url"], "kind": first["kind"]}

        profile.last_components = components
        profile.last_checked_at = self.clock()
        self._save(profile)
        return result

    # -- steps ----------------------------------------------------------------

    def _check_site(
        self, main, profile, components, get, record, budget, low, high
    ) -> None:
        charset = "utf-8"
        match = re.search(r"charset=([\w-]+)", main.content_type or "", re.IGNORECASE)
        if match:
            charset = match.group(1)
        is_html = "html" in (main.content_type or "").lower() or main.body.lstrip()[
            :15
        ].lower().startswith((b"<!doctype", b"<html"))
        info = PageInfo()
        if is_html:
            try:
                html = main.body.decode(charset, errors="replace")
            except LookupError:
                html = main.body.decode("utf-8", errors="replace")
            info = analyze_html(html, main.final_url, self.limits)

        fingerprint = hashlib.sha256("\n".join(info.scripts).encode()).hexdigest()[:16]
        now = self.clock()
        stale = (
            profile.discovered_at is None
            or now - profile.discovered_at
            > timedelta(seconds=self.limits.rediscover_seconds)
            or profile.fingerprint != fingerprint
        )
        bundle_bodies: dict[str, bytes] = {}

        # 2. own assets (fresh from the current HTML on every check)
        disabled = set(profile.disabled)
        scan_bundles = (self.force_discover or stale) and is_html
        for asset in info.assets:
            if redact_url(asset) in disabled:
                record(asset, "asset", "html", None, None, "disabled")
                continue
            if budget.remaining() <= 0:
                record(asset, "asset", "html", None, None, "skipped")
                continue
            want_body = (
                scan_bundles
                and asset in info.scripts
                and len(bundle_bodies) < self.limits.max_bundles
            )
            fetched = get(
                asset,
                min(self.backend_timeout, 30.0),
                self.limits.max_bundle_bytes if want_body else 0,
            )
            ok = fetched.status is not None and 200 <= fetched.status < 400
            self._record_probe(record, asset, "asset", "html", fetched, ok)
            if want_body and ok:
                bundle_bodies[asset] = fetched.body

        # 4. discovery of backends
        if scan_bundles:
            scored: dict[str, int] = dict.fromkeys(info.preconnect_origins, 2)
            for text in [info.inline_js] + [
                b.decode("utf-8", "replace") for b in bundle_bodies.values()
            ]:
                for origin, score in find_backend_origins(text, main.final_url):
                    scored[origin] = max(scored.get(origin, 0), score)
            ranked = sorted(scored.items(), key=lambda item: -item[1])
            profile.backends = [o for o, _ in ranked][: self.limits.max_backends]
            profile.discovered_at = now
            profile.fingerprint = fingerprint

        # 3. same-site pages
        manual_pages = [m["url"] for m in profile.manual if m["kind"] == "page"]
        for page in info.pages:
            self._probe(page, "page", "html", disabled, get, record, budget, low, high)
        for page in manual_pages[: self.limits.max_manual]:
            self._probe(
                page, "page", "manual", disabled, get, record, budget, low, high
            )

        self._probe_backends(profile, components, get, record, budget)

    def _probe(
        self, url, kind, source, disabled, get, record, budget, low, high
    ) -> None:
        if redact_url(url) in disabled:
            record(url, kind, source, None, None, "disabled")
            return
        if budget.remaining() <= 0:
            record(url, kind, source, None, None, "skipped")
            return
        fetched = get(url, 30.0)
        ok = fetched.status is not None and low <= fetched.status <= high
        self._record_probe(record, url, kind, source, fetched, ok)

    def _probe_backends(self, profile, components, get, record, budget) -> None:
        disabled = set(profile.disabled)
        targets = [(o, "discovered") for o in profile.backends]
        targets += [
            (m["url"], "manual") for m in profile.manual if m["kind"] == "backend"
        ][: self.limits.max_manual]
        seen: set[str] = set()
        for target, source in targets:
            key = redact_url(target)
            if key in seen:
                continue
            seen.add(key)
            if key in disabled:
                record(target, "backend", source, None, None, "disabled")
                continue
            if budget.remaining() <= 0:
                record(target, "backend", source, None, None, "skipped")
                continue
            fetched = get(target, self.backend_timeout)
            # Any HTTP answer below 500 means the server is up (404/401/405 too).
            ok = fetched.status is not None and fetched.status < 500
            self._record_probe(record, target, "backend", source, fetched, ok)

    @staticmethod
    def _record_probe(
        record, url, kind, source, fetched: FetchResult, ok: bool
    ) -> None:
        if fetched.error_type == ErrorType.BLOCKED_DESTINATION:
            # Never contacted (SSRF guard). A backend the public site's own code
            # calls on a private address is broken for every visitor, so it
            # counts as down; anything else is just reported as blocked.
            if kind == "backend" and source == "discovered":
                fetched.error_type = PRIVATE_BACKEND
                fetched.status = None
                record(url, kind, source, fetched, False)
            else:
                record(url, kind, source, fetched, None, "blocked")
            return
        if not ok and fetched.status is not None and fetched.error_type is None:
            fetched.error_type = ErrorType.HTTP_STATUS.value
            fetched.error = f"HTTP error: {fetched.status}"
        record(url, kind, source, fetched, ok)

    def _save(self, profile: SiteProfile) -> None:
        """Persist discovery + last results, keeping concurrent user edits."""
        try:
            latest = SiteProfile.from_dict(
                profile.service, self.storage.get_site_profile(profile.service)
            )
            profile.manual = latest.manual
            profile.disabled = latest.disabled
            self.storage.save_site_profile(profile.service, profile.to_dict())
        except StorageError as exc:
            logger.warning(
                "Saving site profile for %s failed: %s", profile.service, exc
            )
