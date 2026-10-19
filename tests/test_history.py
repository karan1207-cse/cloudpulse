from cloudpulse.history import HealthCheckHistory


def test_history_starts_empty():
    history = HealthCheckHistory()

    assert history.count() == 0
    assert history.get_all() == []


def test_history_stores_results():
    history = HealthCheckHistory()
    result = {
        "url": "https://example.com",
        "is_healthy": True,
        "status_code": 200,
    }

    history.add(result)

    assert history.count() == 1
    assert history.get_all()[0]["status_code"] == 200


def test_history_returns_copies():
    history = HealthCheckHistory()
    history.add({"url": "https://example.com", "is_healthy": True})

    records = history.get_all()
    records.clear()

    assert history.count() == 1
