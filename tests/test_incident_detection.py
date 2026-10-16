
from cloudpulse.incident_manager import ServiceIncidentTracker


def test_incident_opens_after_three_failures():
    tracker = ServiceIncidentTracker("https://example.com")

    assert tracker.record_failure() is None
    assert tracker.record_failure() is None

    incident = tracker.record_failure()

    assert incident is not None
    assert incident.status == "OPEN"


def test_repeated_failures_do_not_create_duplicate_incidents():
    tracker = ServiceIncidentTracker("https://example.com")

    tracker.record_failure()
    tracker.record_failure()
    first_incident = tracker.record_failure()

    second_incident = tracker.record_failure()

    assert second_incident is first_incident


def test_success_resets_counter_and_recovers_incident():
    tracker = ServiceIncidentTracker("https://example.com")

    tracker.record_failure()
    tracker.record_failure()
    incident = tracker.record_failure()

    recovered = tracker.record_success()

    assert recovered is incident
    assert recovered.status == "RECOVERED"
    assert tracker.consecutive_failures == 0
    assert tracker.active_incident is None
