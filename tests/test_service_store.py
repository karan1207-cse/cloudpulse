"""Service store contract (memory, JSON, DynamoDB) and the service registry."""

import json
import socket

import pytest

from cloudpulse.config import MAX_SERVICES, ServiceConfig, load_config
from cloudpulse.errors import (
    ConcurrencyConflictError,
    ServiceNotFoundError,
    ServiceReadOnlyError,
    ServiceValidationError,
    StorageCorruptError,
)
from cloudpulse.service_registry import ServiceRegistry
from cloudpulse.service_store import (
    DynamoDBServiceStore,
    InMemoryServiceStore,
    JsonServiceStore,
    ServiceStore,
)
from conftest import SERVICES_TABLE


@pytest.fixture(params=["memory", "json", "dynamodb"])
def store(request, tmp_path, clock):
    if request.param == "memory":
        return InMemoryServiceStore(clock)
    if request.param == "json":
        return JsonServiceStore(tmp_path / "services.json", clock)
    resource = request.getfixturevalue("dynamodb")
    return DynamoDBServiceStore(resource.Table(SERVICES_TABLE), clock)


def svc(name="api", url="https://api.example.com/health", **kwargs):
    return ServiceConfig(name=name, url=url, **kwargs)


# -- store contract ------------------------------------------------------------


def test_store_satisfies_protocol(store):
    assert isinstance(store, ServiceStore)


def test_store_round_trip(store):
    service = svc(timeout_seconds=2.5, severity="CRITICAL", accepted_status=(200, 204))
    store.add_service(service)

    assert store.get_service("api") == service
    assert store.get_service("missing") is None
    assert store.list_services() == [service]


def test_store_lists_sorted_by_name(store):
    for name in ("zeta", "alpha", "mid"):
        store.add_service(svc(name=name))

    assert [s.name for s in store.list_services()] == ["alpha", "mid", "zeta"]


def test_store_rejects_duplicates(store):
    store.add_service(svc())

    with pytest.raises(ConcurrencyConflictError):
        store.add_service(svc(url="https://other.example.com/"))
    assert store.get_service("api").url == "https://api.example.com/health"


def test_store_replace_and_delete(store):
    store.add_service(svc())

    assert store.replace_service(svc(failure_threshold=7, enabled=False)) is True
    assert store.get_service("api").failure_threshold == 7
    assert store.get_service("api").enabled is False
    assert store.replace_service(svc(name="missing")) is False
    assert store.delete_service("api") is True
    assert store.delete_service("api") is False
    assert store.list_services() == []


def test_json_store_keeps_unredacted_url_and_timestamps(tmp_path, clock):
    store = JsonServiceStore(tmp_path / "services.json", clock)
    store.add_service(svc(url="https://api.example.com/h?key=abc"))
    clock.advance(seconds=60)
    store.replace_service(svc(url="https://api.example.com/h?key=xyz"))

    record = json.loads((tmp_path / "services.json").read_text())["api"]
    assert record["url"].endswith("key=xyz")
    assert record["created_at"] == "2026-03-01T12:00:00+00:00"
    assert record["updated_at"] == "2026-03-01T12:01:00+00:00"


def test_corrupt_stored_service_is_reported(tmp_path):
    path = tmp_path / "services.json"
    path.write_text(json.dumps({"api": {"name": "api", "url": "ftp://x"}}))

    with pytest.raises(StorageCorruptError, match="api"):
        JsonServiceStore(path).list_services()


def test_corrupt_services_file_is_not_overwritten(tmp_path):
    path = tmp_path / "services.json"
    path.write_text("{oops")

    with pytest.raises(StorageCorruptError):
        JsonServiceStore(path).add_service(svc())
    assert path.read_text() == "{oops"


# -- registry ---------------------------------------------------------------------


@pytest.fixture
def registry(fake_dns, clock):
    config = load_config(
        {
            "CLOUDPULSE_SERVICES": json.dumps(
                [{"name": "from-file", "url": "https://file.example.com/"}]
            )
        }
    )
    return ServiceRegistry(config, InMemoryServiceStore(clock))


def test_registry_merges_config_and_store(registry):
    registry.create({"name": "web", "url": "https://web.example.com/"})

    entries = registry.entries()
    assert [(e.service.name, e.source, e.editable) for e in entries] == [
        ("from-file", "config", False),
        ("web", "dashboard", True),
    ]
    assert [s.name for s in registry.enabled_services()] == ["from-file", "web"]


def test_registry_applies_defaults_and_trims(registry):
    service, warnings = registry.create(
        {"name": "  web ", "url": " https://web.example.com/ "}
    )

    assert service.name == "web"
    assert service.url == "https://web.example.com/"
    assert service.timeout_seconds == 5.0
    assert service.failure_threshold == 3
    assert service.accepted_status == (200, 399)
    assert warnings == []


@pytest.mark.parametrize(
    ("data", "field", "text"),
    [
        ({"url": "https://x.example.com"}, "name", "name is required"),
        ({"name": "has space", "url": "https://x.example.com"}, "name", "letters"),
        ({"name": "x"}, "url", "URL is required"),
        ({"name": "x", "url": "notaurl"}, "url", "HTTP or HTTPS"),
        ({"name": "x", "url": "ftp://x.example.com"}, "url", "HTTP or HTTPS"),
        ({"name": "x", "url": "http://127.0.0.1/"}, "url", "public internet"),
        ({"name": "x", "url": "http://169.254.169.254/"}, "url", "public internet"),
        ({"name": "x", "url": "https://u:p@x.example.com/"}, "url", "credentials"),
        (
            {"name": "x", "url": "https://x.example.com", "timeout_seconds": 0},
            "timeout_seconds",
            "Timeout",
        ),
        (
            {"name": "x", "url": "https://x.example.com", "failure_threshold": 0},
            "failure_threshold",
            "whole number",
        ),
        (
            {"name": "x", "url": "https://x.example.com", "severity": "URGENT"},
            "severity",
            "Severity",
        ),
        (
            {
                "name": "x",
                "url": "https://x.example.com",
                "accepted_status": [500, 200],
            },
            "accepted_status",
            "lowest first",
        ),
        ({"name": "x", "url": "https://x.example.com", "evil": 1}, None, "unknown"),
    ],
)
def test_registry_validation_errors_are_field_specific(registry, data, field, text):
    with pytest.raises(ServiceValidationError) as exc:
        registry.create(data)

    assert exc.value.field == field
    assert text in exc.value.detail


def test_registry_blocks_hostnames_resolving_to_private_addresses(registry, fake_dns):
    fake_dns.mapping["intranet.example.com"] = ["10.0.0.7"]

    with pytest.raises(ServiceValidationError) as exc:
        registry.create({"name": "intra", "url": "https://intranet.example.com/"})

    assert exc.value.field == "url"
    assert "10.0.0.7" in exc.value.detail
    assert registry.get("intra") is None


def test_registry_warns_but_saves_when_dns_fails(registry, fake_dns):
    fake_dns.mapping["not-yet-live.example.com"] = socket.gaierror(-2, "unknown")

    _service, warnings = registry.create(
        {"name": "soon", "url": "https://not-yet-live.example.com/"}
    )

    assert registry.get("soon") is not None
    assert "could not look up" in warnings[0]


def test_registry_rejects_duplicate_names_including_config(registry):
    registry.create({"name": "web", "url": "https://web.example.com/"})

    for name in ("web", "from-file"):
        with pytest.raises(ServiceValidationError, match="already exists") as exc:
            registry.create({"name": name, "url": "https://x.example.com/"})
        assert exc.value.field == "name"


def test_registry_limit_counts_all_sources(registry):
    for i in range(MAX_SERVICES - 1):  # plus one config service = limit
        registry.create({"name": f"s{i}", "url": f"https://s{i}.example.com/"})

    with pytest.raises(ServiceValidationError, match="at most 50") as exc:
        registry.create({"name": "one-too-many", "url": "https://x.example.com/"})
    assert exc.value.field is None


def test_registry_update_and_delete(registry):
    registry.create({"name": "web", "url": "https://web.example.com/"})

    service, _ = registry.update(
        "web", {"url": "https://web.example.com/v2", "failure_threshold": 5}
    )
    assert service.failure_threshold == 5
    assert registry.get("web").service.url.endswith("/v2")

    with pytest.raises(ServiceValidationError, match="renamed"):
        registry.update("web", {"name": "other", "url": "https://web.example.com/"})

    registry.delete("web")
    assert registry.get("web") is None
    with pytest.raises(ServiceNotFoundError):
        registry.delete("web")
    with pytest.raises(ServiceNotFoundError):
        registry.update("web", {"url": "https://web.example.com/"})


def test_config_services_are_read_only(registry):
    with pytest.raises(ServiceReadOnlyError):
        registry.update("from-file", {"url": "https://x.example.com/"})
    with pytest.raises(ServiceReadOnlyError):
        registry.delete("from-file")


def test_registry_without_store_is_read_only(fake_dns):
    registry = ServiceRegistry(load_config({}), None)

    assert registry.can_store is False
    with pytest.raises(ServiceReadOnlyError, match="CLOUDPULSE_SERVICES_TABLE"):
        registry.create({"name": "x", "url": "https://x.example.com/"})


def test_import_copies_config_services_once(registry):
    assert registry.import_config_services() == ["from-file"]
    assert registry.import_config_services() == []
    assert registry.store.get_service("from-file") is not None


def test_config_entry_wins_over_store(registry, clock):
    registry.store.add_service(svc(name="from-file", url="https://store.example.com/"))

    entry = registry.get("from-file")
    assert entry.source == "config"
    assert entry.service.url == "https://file.example.com/"
    assert len(registry.entries()) == 1


def test_private_targets_allowed_only_when_configured(fake_dns, clock):
    config = load_config({"CLOUDPULSE_ALLOW_PRIVATE_TARGETS": "true"})
    registry = ServiceRegistry(config, InMemoryServiceStore(clock))

    service, _ = registry.create({"name": "local", "url": "http://127.0.0.1:8080/"})

    assert service.url == "http://127.0.0.1:8080/"
