
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

def test_success_before_threshold_resets_failure_counter():
    tracker = ServiceIncidentTracker(
        "https://example.com",
        failure_threshold=3,
    )

    tracker.record_failure()
    tracker.record_failure()

    assert tracker.record_success() is None
    assert tracker.consecutive_failures == 0
    assert tracker.active_incident is None

    assert tracker.record_failure() is None
    assert tracker.record_failure() is None


def test_custom_threshold_opens_incident_after_two_failures():
    tracker = ServiceIncidentTracker(
        "https://example.com",
        failure_threshold=2,
    )

    assert tracker.record_failure() is None

    incident = tracker.record_failure()

    assert incident is not None
    assert incident.status == "OPEN"
