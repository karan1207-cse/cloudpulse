"""Scheduler leases, background checking and double-run protection."""

import json
import threading
from datetime import timedelta
from unittest.mock import patch

import pytest

from cloudpulse import cli
from cloudpulse.config import load_config
from cloudpulse.dynamodb_incident_storage import DynamoDBIncidentStorage
from cloudpulse.factory import Dependencies
from cloudpulse.history import HealthCheckHistory
from cloudpulse.incident_storage import InMemoryIncidentStorage, JsonIncidentStorage
from cloudpulse.notifications import RecordingNotifier
from cloudpulse.scheduler import (
    SCHEDULER_LEASE,
    BackgroundScheduler,
    MonitorRunner,
    lease_owner_id,
)
from cloudpulse.service_registry import ServiceRegistry
from cloudpulse.service_store import InMemoryServiceStore
from conftest import INCIDENTS_TABLE, STATE_TABLE, FakeTransport, ScriptedChecker


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


# -- leases ---------------------------------------------------------------------


def test_lease_is_exclusive_until_expiry(storage, clock):
    now = clock()
    assert storage.acquire_lease("scheduler", "a", 60, now) is True
    assert storage.acquire_lease("scheduler", "b", 60, now) is False
    # The holder can renew.
    assert storage.acquire_lease("scheduler", "a", 60, now + timedelta(seconds=30))
    assert (
        storage.acquire_lease("scheduler", "b", 60, now + timedelta(seconds=80))
        is False
    )
    # Expired: someone else may take over.
    assert storage.acquire_lease("scheduler", "b", 60, now + timedelta(seconds=100))
    assert (
        storage.acquire_lease("scheduler", "a", 60, now + timedelta(seconds=101))
        is False
    )


def test_lease_release_only_by_owner(storage, clock):
    now = clock()
    storage.acquire_lease("scheduler", "a", 60, now)

    storage.release_lease("scheduler", "b")
    assert storage.acquire_lease("scheduler", "b", 60, now) is False

    storage.release_lease("scheduler", "a")
    assert storage.acquire_lease("scheduler", "b", 60, now) is True


def test_leases_are_not_incidents_or_states(storage, clock):
    storage.acquire_lease("scheduler", "a", 60, clock())

    assert storage.count() == 0
    assert storage.get_state("scheduler") is None


def test_json_lease_is_shared_between_processes(tmp_path, clock):
    # Two storage objects on the same files behave like two processes.
    first = JsonIncidentStorage(tmp_path / "incidents.json")
    second = JsonIncidentStorage(tmp_path / "incidents.json")

    assert first.acquire_lease("scheduler", "serve", 60, clock())
    assert not second.acquire_lease("scheduler", "cli", 60, clock())
    assert not (tmp_path / "incidents_state.json.lock").exists()


def test_concurrent_acquire_has_single_winner(tmp_path, clock):
    path = tmp_path / "incidents.json"
    winners = []

    def contend(owner):
        if JsonIncidentStorage(path).acquire_lease("scheduler", owner, 60, clock()):
            winners.append(owner)

    threads = [threading.Thread(target=contend, args=(f"o{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1


def test_owner_ids_are_unique():
    assert lease_owner_id("serve") != lease_owner_id("serve")


# -- background scheduler -----------------------------------------------------------


@pytest.fixture
def runner(fake_dns, clock):
    config = load_config(
        {
            "CLOUDPULSE_SERVICES": json.dumps(
                [{"name": "api", "url": "https://api.example.com/"}]
            )
        }
    )
    registry = ServiceRegistry(config, InMemoryServiceStore(clock))
    deps = Dependencies(
        history=HealthCheckHistory(),
        incidents=InMemoryIncidentStorage(),
        notifier=RecordingNotifier(),
        checker=ScriptedChecker(clock, True),  # type: ignore[arg-type]
        clock=clock,
    )
    return MonitorRunner(registry, deps, config)


def test_tick_checks_all_enabled_services_including_new_ones(runner, clock):
    scheduler = BackgroundScheduler(runner, 60, clock=clock)
    runner.registry.create({"name": "web", "url": "https://web.example.com/"})
    runner.registry.create(
        {"name": "paused", "url": "https://p.example.com/", "enabled": False}
    )

    summary = scheduler.tick()

    assert sorted(o.service for o in summary.outcomes) == ["api", "web"]
    assert scheduler.is_leader is True
    status = scheduler.status()
    assert status["last_run_at"] == "2026-03-01T12:00:00+00:00"
    assert status["last_result"] == {"checked": 2, "unhealthy": 0, "failures": 0}


def test_tick_skips_when_another_scheduler_holds_lease(runner, clock):
    runner.deps.incidents.acquire_lease(SCHEDULER_LEASE, "cli:other", 300, clock())
    scheduler = BackgroundScheduler(runner, 60, clock=clock)

    assert scheduler.tick() is None
    assert scheduler.is_leader is False
    assert runner.deps.checker.calls == 0


def test_two_schedulers_never_both_run(runner, clock):
    first = BackgroundScheduler(runner, 60, clock=clock)
    second = BackgroundScheduler(runner, 60, clock=clock)

    assert first.tick() is not None
    assert second.tick() is None
    clock.advance(seconds=60)
    assert first.tick() is not None  # renewal by the holder
    assert second.tick() is None


def test_scheduler_skips_services_checked_moments_ago(runner, clock):
    scheduler = BackgroundScheduler(runner, 60, clock=clock)
    runner.check_service("api")  # "Check now" a few seconds before the tick
    clock.advance(seconds=5)

    summary = scheduler.tick()

    assert summary.outcomes[0].skipped is True
    clock.advance(seconds=60)
    assert scheduler.tick().outcomes[0].skipped is False


def test_thread_runs_and_stops_releasing_lease(runner, clock):
    scheduler = BackgroundScheduler(runner, 60, clock=clock)
    ran = threading.Event()
    original = scheduler.tick

    def tick():
        result = original()
        ran.set()
        return result

    scheduler.tick = tick
    scheduler.start()
    assert ran.wait(5)
    scheduler.stop()

    assert scheduler.is_leader is False
    # Lease released: another owner can take it immediately.
    assert runner.deps.incidents.acquire_lease(SCHEDULER_LEASE, "next", 60, clock())


def test_failing_round_is_recorded_not_fatal(runner, clock):
    scheduler = BackgroundScheduler(runner, 60, clock=clock)
    done = threading.Event()

    def boom():
        done.set()
        raise RuntimeError("storage down")

    scheduler.tick = boom
    scheduler.start()
    assert done.wait(5)
    scheduler.stop()

    assert "storage down" in scheduler.status()["last_error"]


def test_interval_must_be_positive(runner):
    with pytest.raises(ValueError):
        BackgroundScheduler(runner, 0)


# -- CLI interplay --------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path, monkeypatch, fake_dns):
    for key in list(__import__("os").environ):
        if key.startswith("CLOUDPULSE_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("CLOUDPULSE_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def test_run_loop_refuses_when_serve_scheduler_is_active(cli_env, capsys, clock):
    cli.main(["services", "add", "api", "https://api.example.com/"])
    storage = JsonIncidentStorage(cli_env / "data" / "incidents.json")
    from datetime import UTC, datetime

    storage.acquire_lease(SCHEDULER_LEASE, "serve:other", 300, datetime.now(UTC))

    with patch("cloudpulse.health_check.urlopen", side_effect=FakeTransport(200)):
        code = cli.main(["run", "--loop", "--interval", "1"])

    assert code == cli.EXIT_USAGE
    assert "Another CloudPulse scheduler" in capsys.readouterr().err


def test_run_loop_takes_and_releases_lease(cli_env, capsys):
    cli.main(["services", "add", "api", "https://api.example.com/"])
    with (
        patch("cloudpulse.health_check.urlopen", side_effect=FakeTransport(200)),
        patch("cloudpulse.cli.time.sleep", side_effect=KeyboardInterrupt),
    ):
        assert cli.main(["run", "--loop", "--interval", "1"]) == cli.EXIT_INTERRUPTED

    from datetime import UTC, datetime

    storage = JsonIncidentStorage(cli_env / "data" / "incidents.json")
    assert storage.acquire_lease(SCHEDULER_LEASE, "serve", 60, datetime.now(UTC))


def test_services_commands(cli_env, capsys, monkeypatch):
    assert cli.main(["services", "list"]) == 0
    assert "No services configured yet" in capsys.readouterr().out

    assert (
        cli.main(
            [
                "services",
                "add",
                "shop",
                "https://shop.example.com/?k=secret",
                "--threshold",
                "2",
            ]
        )
        == 0
    )
    assert "Added shop (https://shop.example.com/)" in capsys.readouterr().out

    assert (
        cli.main(["services", "add", "shop", "https://x.example.com/"])
        == cli.EXIT_USAGE
    )
    assert "already exists" in capsys.readouterr().err

    assert (
        cli.main(["services", "add", "lan", "http://192.168.1.10/"]) == cli.EXIT_USAGE
    )
    assert "public internet" in capsys.readouterr().err

    assert cli.main(["services", "list", "--json"]) == 0
    [listed] = json.loads(capsys.readouterr().out)
    assert listed["failure_threshold"] == 2 and listed["source"] == "dashboard"
    assert "secret" not in listed["url"]

    with patch("cloudpulse.health_check.urlopen", side_effect=FakeTransport(200)):
        assert cli.main(["run"]) == 0  # CLI sees services added from anywhere

    assert cli.main(["services", "remove", "shop"]) == 0
    assert cli.main(["services", "remove", "shop"]) == cli.EXIT_USAGE


def test_services_import_from_file(cli_env, capsys, monkeypatch):
    file = cli_env / "services.json"
    file.write_text(
        json.dumps([{"name": "legacy", "url": "https://legacy.example.com/"}])
    )
    monkeypatch.setenv("CLOUDPULSE_SERVICES_FILE", str(file))

    assert cli.main(["services", "remove", "legacy"]) == cli.EXIT_USAGE
    assert "services file" in capsys.readouterr().err

    assert cli.main(["services", "import"]) == 0
    assert "Imported 1 service(s): legacy" in capsys.readouterr().out
