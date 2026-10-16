
"""Application health-check utilities."""

import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def check_health(url: str, timeout: float = 5.0) -> dict:
    """Check an HTTP endpoint and return its health-check result."""
    started_at = time.perf_counter()

    result = {
        "url": url,
        "is_healthy": False,
        "status_code": None,
        "response_time_ms": 0.0,
        "error": None,
    }

    try:
        parsed_url = urlparse(url)

        if (
            parsed_url.scheme not in ("http", "https")
            or not parsed_url.hostname
        ):
            raise ValueError("A valid HTTP or HTTPS URL is required.")

        if timeout <= 0:
            raise ValueError("Timeout must be greater than zero.")

        request = Request(url, method="GET")

        with urlopen(request, timeout=timeout) as response:
            result["status_code"] = response.status
            result["is_healthy"] = 200 <= response.status < 400

    except HTTPError as exc:
        result["status_code"] = exc.code
        result["error"] = f"HTTP error: {exc.code}"

    except (URLError, TimeoutError, OSError, ValueError) as exc:
        result["error"] = str(exc)

    finally:
        result["response_time_ms"] = round(
            (time.perf_counter() - started_at) * 1000, 2
        )

    return result
