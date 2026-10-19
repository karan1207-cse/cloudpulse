"""Validated configuration from environment variables and a services file.

Every setting has a safe default for local use; the AWS backend additionally
requires table names. See ``.env.example`` for the full list.
"""

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cloudpulse.errors import ConfigError, ServiceValidationError, UnsafeURLError
from cloudpulse.health_check import validate_accepted_status
from cloudpulse.incident_manager import SEVERITIES
from cloudpulse.url_safety import BlockedDestinationError, UrlPolicy

MAX_SERVICES = 50
MAX_SERVICE_TIMEOUT = 30.0
MAX_THRESHOLD = 100
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ENVIRONMENTS = ("local", "production")
BACKENDS = ("local", "memory", "aws")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_SERVICE_KEYS = {
    "name",
    "url",
    "timeout_seconds",
    "failure_threshold",
    "severity",
    "accepted_status",
    "escalation_threshold",
    "enabled",
    "check_mode",
    "backend_timeout_seconds",
}
CHECK_MODES = ("quick", "full")
MAX_BACKEND_TIMEOUT = 60.0
PRIVATE_TARGET_HINT = (
    "For security, CloudPulse only monitors public internet addresses "
    "(not localhost, private networks or cloud metadata endpoints)."
)
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


@dataclass(frozen=True)
class ServiceConfig:
    """One monitored endpoint."""

    name: str
    url: str
    timeout_seconds: float = 5.0
    failure_threshold: int = 3
    severity: str = "HIGH"
    accepted_status: tuple[int, int] = (200, 399)
    escalation_threshold: int | None = None
    enabled: bool = True
    # "quick": only this URL. "full": the page, its own assets, a few
    # same-site pages and the backend APIs its JavaScript talks to.
    check_mode: str = "quick"
    backend_timeout_seconds: float = 20.0

    @property
    def max_check_seconds(self) -> float:
        """Upper bound of one check, used for time budgets."""
        if self.check_mode == "full":
            from cloudpulse.site_check import FULL_CHECK_BUDGET_SECONDS

            return FULL_CHECK_BUDGET_SECONDS
        return self.timeout_seconds

    def to_storage_dict(self) -> dict[str, Any]:
        """Full definition (including the unredacted URL) for persistence."""
        return {
            "name": self.name,
            "url": self.url,
            "timeout_seconds": self.timeout_seconds,
            "failure_threshold": self.failure_threshold,
            "severity": self.severity,
            "accepted_status": list(self.accepted_status),
            "escalation_threshold": self.escalation_threshold,
            "enabled": self.enabled,
            "check_mode": self.check_mode,
            "backend_timeout_seconds": self.backend_timeout_seconds,
        }

    def to_public_dict(self) -> dict[str, Any]:
        from cloudpulse.url_safety import redact_url

        return {
            "name": self.name,
            "url": redact_url(self.url),
            "timeout_seconds": self.timeout_seconds,
            "failure_threshold": self.failure_threshold,
            "severity": self.severity,
            "accepted_status": list(self.accepted_status),
            "escalation_threshold": self.escalation_threshold,
            "enabled": self.enabled,
            "check_mode": self.check_mode,
            "backend_timeout_seconds": self.backend_timeout_seconds,
        }


@dataclass(frozen=True)
class AppConfig:
    environment: str = "local"
    backend: str = "local"
    data_dir: Path = Path("data")
    services: tuple[ServiceConfig, ...] = ()
    default_timeout: float = 5.0
    default_failure_threshold: int = 3
    allow_private_targets: bool = False
    max_redirects: int = 5
    aws_region: str | None = None
    health_table: str | None = None
    incidents_table: str | None = None
    state_table: str | None = None
    sns_topic_arn: str | None = None
    history_retention_days: int = 30
    min_check_interval_seconds: float = 0.0
    log_level: str = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    cors_origins: tuple[str, ...] = field(
        default=("http://localhost:5173", "http://127.0.0.1:5173")
    )
    services_table: str | None = None
    check_interval_seconds: float = 60.0
    # Never printed or logged (repr=False); compared in constant time.
    admin_token: str | None = field(default=None, repr=False)

    @property
    def url_policy(self) -> UrlPolicy:
        return UrlPolicy(allow_private=self.allow_private_targets)

    def service(self, name: str) -> ServiceConfig | None:
        return next((s for s in self.services if s.name == name), None)

    @property
    def enabled_services(self) -> tuple[ServiceConfig, ...]:
        return tuple(s for s in self.services if s.enabled)


# -- parsing helpers ----------------------------------------------------------


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{key} must be true or false, got {raw!r}.")


def _number(
    env: Mapping[str, str],
    key: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
    integer: bool = False,
) -> Any:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value: float = int(raw) if integer else float(raw)
    except ValueError as exc:
        kind = "an integer" if integer else "a number"
        raise ConfigError(f"{key} must be {kind}, got {raw!r}.") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}.")
    return value


def _choice(env: Mapping[str, str], key: str, default: str, choices: tuple) -> str:
    value = (env.get(key) or default).strip()
    value = value.upper() if key == "CLOUDPULSE_LOG_LEVEL" else value.lower()
    if value not in choices:
        raise ConfigError(f"{key} must be one of {', '.join(choices)}, got {value!r}.")
    return value


def _optional(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    return value.strip() if value and value.strip() else None


def parse_service(
    raw: Any,
    *,
    index: int | None,
    default_timeout: float,
    default_threshold: int,
    policy: UrlPolicy,
) -> ServiceConfig:
    """Validate one service definition.

    Raises :class:`ServiceValidationError` whose ``field`` and ``detail`` are
    suitable for showing next to a form input.
    """
    where = f"services[{index}]" if index is not None else None

    def fail(field: str | None, detail: str) -> ServiceValidationError:
        return ServiceValidationError(field, detail, where)

    if not isinstance(raw, dict):
        raise fail(None, "Each service must be an object.")
    unknown = set(raw) - _SERVICE_KEYS
    if unknown:
        raise fail(None, f"Service has unknown keys: {', '.join(sorted(unknown))}.")

    if "name" not in raw or raw["name"] in (None, ""):
        raise fail("name", "A name is required.")
    name = raw["name"]
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise fail(
            "name",
            "The name must be 1-64 characters: letters, digits, '.', '_' or '-', "
            "starting with a letter or digit.",
        )

    url = raw.get("url")
    if url in (None, ""):
        raise fail("url", "A URL is required, e.g. https://example.com/health.")
    try:
        policy.check_url(url)
    except BlockedDestinationError as exc:
        raise fail("url", f"{exc} {PRIVATE_TARGET_HINT}") from exc
    except UnsafeURLError as exc:
        raise fail("url", str(exc)) from exc

    timeout = raw.get("timeout_seconds", default_timeout)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not (0 < timeout <= MAX_SERVICE_TIMEOUT)
    ):
        raise fail(
            "timeout_seconds",
            f"Timeout must be more than 0 and at most {MAX_SERVICE_TIMEOUT:g} seconds.",
        )

    threshold = raw.get("failure_threshold", default_threshold)
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, int)
        or not (1 <= threshold <= MAX_THRESHOLD)
    ):
        raise fail(
            "failure_threshold",
            f"Failure threshold must be a whole number from 1 to {MAX_THRESHOLD}.",
        )

    severity = raw.get("severity", "HIGH")
    if severity not in SEVERITIES:
        raise fail("severity", f"Severity must be one of {', '.join(SEVERITIES)}.")

    accepted = raw.get("accepted_status", [200, 399])
    try:
        if not isinstance(accepted, list | tuple) or len(accepted) != 2:
            raise ValueError("must be [low, high]")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in accepted):
            raise ValueError("values must be integers")
        accepted_range = validate_accepted_status((accepted[0], accepted[1]))
    except ValueError as exc:
        raise fail(
            "accepted_status",
            f"accepted_status {exc}. Use two HTTP codes between 100 and 599, "
            "lowest first (e.g. 200 and 399).",
        ) from exc

    escalation = raw.get("escalation_threshold")
    if escalation is not None and (
        isinstance(escalation, bool)
        or not isinstance(escalation, int)
        or not threshold < escalation <= MAX_THRESHOLD * 10
    ):
        raise fail(
            "escalation_threshold",
            "escalation_threshold must be a whole number greater than the "
            "failure threshold.",
        )

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise fail("enabled", "enabled must be true or false.")

    check_mode = raw.get("check_mode", "quick")
    if check_mode not in CHECK_MODES:
        raise fail("check_mode", "Check mode must be 'quick' or 'full'.")

    backend_timeout = raw.get("backend_timeout_seconds", 20.0)
    if (
        isinstance(backend_timeout, bool)
        or not isinstance(backend_timeout, int | float)
        or not (1 <= backend_timeout <= MAX_BACKEND_TIMEOUT)
    ):
        raise fail(
            "backend_timeout_seconds",
            f"Backend timeout must be between 1 and {MAX_BACKEND_TIMEOUT:g} seconds.",
        )

    return ServiceConfig(
        name=name,
        url=url,
        timeout_seconds=float(timeout),
        failure_threshold=threshold,
        severity=severity,
        accepted_status=accepted_range,
        escalation_threshold=escalation,
        enabled=enabled,
        check_mode=check_mode,
        backend_timeout_seconds=float(backend_timeout),
    )


def parse_services(
    data: Any, *, default_timeout: float, default_threshold: int, policy: UrlPolicy
) -> tuple[ServiceConfig, ...]:
    if isinstance(data, dict) and "services" in data:
        data = data["services"]
    if not isinstance(data, list):
        raise ConfigError('Services must be a JSON list or {"services": [...]}.')
    if len(data) > MAX_SERVICES:
        raise ConfigError(f"At most {MAX_SERVICES} services may be configured.")
    services = tuple(
        parse_service(
            raw,
            index=i,
            default_timeout=default_timeout,
            default_threshold=default_threshold,
            policy=policy,
        )
        for i, raw in enumerate(data)
    )
    names = [s.name for s in services]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ConfigError(f"Duplicate service names: {', '.join(duplicates)}.")
    return services


def load_services_file(path: Path, **kwargs: Any) -> tuple[ServiceConfig, ...]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Services file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Services file {path} is not valid JSON: {exc}") from exc
    return parse_services(data, **kwargs)


MIN_ADMIN_TOKEN_LENGTH = 32


def _admin_token(env: Mapping[str, str]) -> str | None:
    token = env.get("CLOUDPULSE_ADMIN_TOKEN")
    if token is None or token == "":
        return None
    if len(token) < MIN_ADMIN_TOKEN_LENGTH or any(c.isspace() for c in token):
        raise ConfigError(
            f"CLOUDPULSE_ADMIN_TOKEN must be at least {MIN_ADMIN_TOKEN_LENGTH} "
            "characters without spaces. Generate one with: "
            'python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    return token


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    services_file: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
    load_services: bool = True,
) -> AppConfig:
    """Build and validate an :class:`AppConfig`; raise ``ConfigError`` on problems.

    ``load_services=False`` skips the service list entirely, for commands such
    as a one-off ``check`` that do not use it.
    """
    env = dict(os.environ if env is None else env)
    overrides = dict(overrides or {})

    environment = _choice(env, "CLOUDPULSE_ENV", "local", ENVIRONMENTS)
    backend = overrides.pop("backend", None) or _choice(
        env, "CLOUDPULSE_BACKEND", "local", BACKENDS
    )
    if backend not in BACKENDS:
        raise ConfigError(f"Backend must be one of {', '.join(BACKENDS)}.")

    allow_private = _bool(env, "CLOUDPULSE_ALLOW_PRIVATE_TARGETS", False)
    if overrides.pop("allow_private_targets", False):
        allow_private = True
    if allow_private and environment == "production":
        raise ConfigError(
            "CLOUDPULSE_ALLOW_PRIVATE_TARGETS cannot be enabled when "
            "CLOUDPULSE_ENV=production (SSRF protection is mandatory)."
        )
    policy = UrlPolicy(allow_private=allow_private)

    default_timeout = _number(
        env, "CLOUDPULSE_TIMEOUT_SECONDS", 5.0, minimum=0.1, maximum=MAX_SERVICE_TIMEOUT
    )
    default_threshold = _number(
        env,
        "CLOUDPULSE_FAILURE_THRESHOLD",
        3,
        minimum=1,
        maximum=MAX_THRESHOLD,
        integer=True,
    )
    service_kwargs = {
        "default_timeout": default_timeout,
        "default_threshold": default_threshold,
        "policy": policy,
    }

    services: tuple[ServiceConfig, ...] = ()
    file_value = services_file or _optional(env, "CLOUDPULSE_SERVICES_FILE")
    inline = _optional(env, "CLOUDPULSE_SERVICES")
    if not load_services:
        file_value = inline = None
    if file_value and inline:
        raise ConfigError(
            "Set only one of CLOUDPULSE_SERVICES_FILE and CLOUDPULSE_SERVICES."
        )
    if file_value:
        services = load_services_file(Path(file_value), **service_kwargs)
    elif inline:
        try:
            data = json.loads(inline)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"CLOUDPULSE_SERVICES is not valid JSON: {exc}") from exc
        services = parse_services(data, **service_kwargs)

    sns_topic_arn = _optional(env, "CLOUDPULSE_SNS_TOPIC_ARN")
    if sns_topic_arn and not re.match(
        r"^arn:aws[a-zA-Z-]*:sns:[a-z0-9-]+:\d{12}:[A-Za-z0-9_-]{1,256}$",
        sns_topic_arn,
    ):
        raise ConfigError("CLOUDPULSE_SNS_TOPIC_ARN is not a valid SNS topic ARN.")

    cors = tuple(
        origin.strip()
        for origin in (
            env.get("CLOUDPULSE_CORS_ORIGINS")
            or "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")
        if origin.strip()
    )
    for origin in cors:
        if origin == "*" or not re.match(r"^https?://[A-Za-z0-9.-]+(:\d+)?$", origin):
            raise ConfigError(
                f"CLOUDPULSE_CORS_ORIGINS entry {origin!r} must be an explicit "
                "http(s)://host[:port] origin (wildcards are not allowed)."
            )

    config = AppConfig(
        environment=environment,
        backend=backend,
        data_dir=Path(env.get("CLOUDPULSE_DATA_DIR") or "data"),
        services=services,
        default_timeout=default_timeout,
        default_failure_threshold=default_threshold,
        allow_private_targets=allow_private,
        max_redirects=_number(
            env, "CLOUDPULSE_MAX_REDIRECTS", 5, minimum=0, maximum=10, integer=True
        ),
        aws_region=_optional(env, "AWS_REGION") or _optional(env, "AWS_DEFAULT_REGION"),
        health_table=_optional(env, "CLOUDPULSE_HEALTH_TABLE"),
        incidents_table=_optional(env, "CLOUDPULSE_INCIDENTS_TABLE"),
        state_table=_optional(env, "CLOUDPULSE_STATE_TABLE"),
        sns_topic_arn=sns_topic_arn,
        history_retention_days=_number(
            env,
            "CLOUDPULSE_HISTORY_RETENTION_DAYS",
            30,
            minimum=1,
            maximum=3650,
            integer=True,
        ),
        min_check_interval_seconds=_number(
            env,
            "CLOUDPULSE_MIN_CHECK_INTERVAL_SECONDS",
            0.0,
            minimum=0,
            maximum=86_400,
        ),
        log_level=_choice(env, "CLOUDPULSE_LOG_LEVEL", "INFO", LOG_LEVELS),
        api_host=(env.get("CLOUDPULSE_API_HOST") or "127.0.0.1").strip(),
        api_port=_number(
            env, "CLOUDPULSE_API_PORT", 8000, minimum=1, maximum=65535, integer=True
        ),
        cors_origins=cors,
        services_table=_optional(env, "CLOUDPULSE_SERVICES_TABLE"),
        check_interval_seconds=_number(
            env,
            "CLOUDPULSE_CHECK_INTERVAL_SECONDS",
            60.0,
            minimum=10,
            maximum=86_400,
        ),
        admin_token=_admin_token(env),
    )

    if config.backend == "aws":
        missing = [
            key
            for key, value in (
                ("CLOUDPULSE_HEALTH_TABLE", config.health_table),
                ("CLOUDPULSE_INCIDENTS_TABLE", config.incidents_table),
                ("CLOUDPULSE_STATE_TABLE", config.state_table),
            )
            if not value
        ]
        if missing:
            raise ConfigError("The aws backend requires: " + ", ".join(missing) + ".")
    return config
