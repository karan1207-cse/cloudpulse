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

from cloudpulse.errors import ConfigError, UnsafeURLError
from cloudpulse.health_check import validate_accepted_status
from cloudpulse.incident_manager import SEVERITIES
from cloudpulse.url_safety import UrlPolicy

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
}
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
    index: int,
    default_timeout: float,
    default_threshold: int,
    policy: UrlPolicy,
) -> ServiceConfig:
    where = f"services[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be an object.")
    unknown = set(raw) - _SERVICE_KEYS
    if unknown:
        raise ConfigError(f"{where} has unknown keys: {', '.join(sorted(unknown))}.")

    url = raw.get("url")
    try:
        policy.check_url(url)
    except UnsafeURLError as exc:
        raise ConfigError(f"{where}.url: {exc}") from exc

    if "name" not in raw:
        raise ConfigError(f"{where}.name is required.")
    name = raw["name"]
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise ConfigError(
            f"{where}.name must be 1-64 characters: letters, digits, '.', '_' or '-'."
        )

    timeout = raw.get("timeout_seconds", default_timeout)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not (0 < timeout <= MAX_SERVICE_TIMEOUT)
    ):
        raise ConfigError(
            f"{where}.timeout_seconds must be > 0 and <= {MAX_SERVICE_TIMEOUT}."
        )

    threshold = raw.get("failure_threshold", default_threshold)
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, int)
        or not (1 <= threshold <= MAX_THRESHOLD)
    ):
        raise ConfigError(f"{where}.failure_threshold must be 1..{MAX_THRESHOLD}.")

    severity = raw.get("severity", "HIGH")
    if severity not in SEVERITIES:
        raise ConfigError(f"{where}.severity must be one of {', '.join(SEVERITIES)}.")

    accepted = raw.get("accepted_status", [200, 399])
    try:
        if not isinstance(accepted, list | tuple) or len(accepted) != 2:
            raise ValueError("must be [low, high]")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in accepted):
            raise ValueError("values must be integers")
        accepted_range = validate_accepted_status((accepted[0], accepted[1]))
    except ValueError as exc:
        raise ConfigError(f"{where}.accepted_status {exc}.") from exc

    escalation = raw.get("escalation_threshold")
    if escalation is not None and (
        isinstance(escalation, bool)
        or not isinstance(escalation, int)
        or not threshold < escalation <= MAX_THRESHOLD * 10
    ):
        raise ConfigError(
            f"{where}.escalation_threshold must be an integer greater than "
            "failure_threshold."
        )

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{where}.enabled must be true or false.")

    return ServiceConfig(
        name=name,
        url=url,
        timeout_seconds=float(timeout),
        failure_threshold=threshold,
        severity=severity,
        accepted_status=accepted_range,
        escalation_threshold=escalation,
        enabled=enabled,
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


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    services_file: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> AppConfig:
    """Build and validate an :class:`AppConfig`; raise ``ConfigError`` on problems."""
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
