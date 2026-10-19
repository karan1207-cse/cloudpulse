"""Build storage, notifier and checker objects from configuration."""

from dataclasses import dataclass
from typing import Any

from cloudpulse.clock import Clock, utc_now
from cloudpulse.config import AppConfig, ServiceConfig
from cloudpulse.errors import ConfigError
from cloudpulse.health_check import HttpHealthChecker, Transport
from cloudpulse.history import HealthCheckHistory
from cloudpulse.incident_storage import InMemoryIncidentStorage, JsonIncidentStorage
from cloudpulse.json_storage import JsonHealthCheckStorage
from cloudpulse.monitor import MonitoringService
from cloudpulse.notifications import (
    CompositeNotifier,
    LoggingNotifier,
    Notifier,
    SnsNotifier,
)
from cloudpulse.service_registry import ServiceRegistry
from cloudpulse.service_store import (
    DynamoDBServiceStore,
    InMemoryServiceStore,
    JsonServiceStore,
    ServiceStore,
)
from cloudpulse.storage import HealthCheckStorage, IncidentStorage


@dataclass
class Dependencies:
    history: HealthCheckStorage
    incidents: IncidentStorage
    notifier: Notifier
    checker: HttpHealthChecker
    clock: Clock


def build_storage(
    config: AppConfig, *, dynamodb: Any = None, clock: Clock = utc_now
) -> tuple[HealthCheckStorage, IncidentStorage]:
    """Return ``(history, incidents)`` for the configured backend."""
    if config.backend == "memory":
        return HealthCheckHistory(), InMemoryIncidentStorage()
    if config.backend == "local":
        return (
            JsonHealthCheckStorage(config.data_dir / "health_checks.json"),
            JsonIncidentStorage(config.data_dir / "incidents.json"),
        )

    from cloudpulse.aws_common import dynamodb_resource
    from cloudpulse.dynamodb_incident_storage import DynamoDBIncidentStorage
    from cloudpulse.dynamodb_storage import DynamoDBHealthCheckStorage

    if not (config.health_table and config.incidents_table and config.state_table):
        raise ConfigError("The aws backend requires all three table names.")
    resource = dynamodb or dynamodb_resource(config.aws_region)
    history = DynamoDBHealthCheckStorage(
        config.health_table,
        table=resource.Table(config.health_table),
        retention_days=config.history_retention_days,
        clock=clock,
    )
    incidents = DynamoDBIncidentStorage(
        config.incidents_table,
        table=resource.Table(config.incidents_table),
        state_table=resource.Table(config.state_table),
        state_table_name=config.state_table,
    )
    return history, incidents


def build_service_store(
    config: AppConfig, *, dynamodb: Any = None, clock: Clock = utc_now
) -> ServiceStore | None:
    """Where dashboard-managed services live; ``None`` means config-only."""
    if config.backend == "memory":
        return InMemoryServiceStore(clock)
    if config.backend == "local":
        return JsonServiceStore(config.data_dir / "services.json", clock)
    if not config.services_table:
        return None
    from cloudpulse.aws_common import dynamodb_resource

    resource = dynamodb or dynamodb_resource(config.aws_region)
    return DynamoDBServiceStore(resource.Table(config.services_table), clock)


def build_registry(
    config: AppConfig, *, dynamodb: Any = None, clock: Clock = utc_now
) -> ServiceRegistry:
    return ServiceRegistry(
        config, build_service_store(config, dynamodb=dynamodb, clock=clock)
    )


def build_notifier(config: AppConfig, *, sns_client: Any = None) -> Notifier:
    """Logging always; SNS only for the aws backend with a topic configured.

    Local runs therefore never publish real notifications, even if a topic
    ARN happens to be present in the environment.
    """
    logging_notifier = LoggingNotifier()
    if config.backend == "aws" and config.sns_topic_arn:
        sns = SnsNotifier(
            config.sns_topic_arn, client=sns_client, region_name=config.aws_region
        )
        return CompositeNotifier(logging_notifier, sns)
    return logging_notifier


def build_dependencies(
    config: AppConfig,
    *,
    notifier: Notifier | None = None,
    transport: Transport | None = None,
    clock: Clock = utc_now,
    dynamodb: Any = None,
    sns_client: Any = None,
) -> Dependencies:
    history, incidents = build_storage(config, dynamodb=dynamodb, clock=clock)
    return Dependencies(
        history=history,
        incidents=incidents,
        notifier=notifier or build_notifier(config, sns_client=sns_client),
        checker=HttpHealthChecker(
            policy=config.url_policy,
            transport=transport,
            clock=clock,
            max_redirects=config.max_redirects,
        ),
        clock=clock,
    )


def build_monitor(
    service: ServiceConfig, deps: Dependencies, config: AppConfig
) -> MonitoringService:
    return MonitoringService(
        service_url=service.url,
        service_name=service.name,
        failure_threshold=service.failure_threshold,
        timeout=service.timeout_seconds,
        severity=service.severity,
        accepted_status=service.accepted_status,
        escalation_threshold=service.escalation_threshold,
        history=deps.history,
        incident_storage=deps.incidents,
        checker=deps.checker,
        notifier=deps.notifier,
        clock=deps.clock,
        min_check_interval_seconds=config.min_check_interval_seconds,
    )
