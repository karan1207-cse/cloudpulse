
"""Command-line interface for CloudPulse."""

import argparse
import json

from cloudpulse.monitor import MonitoringService


def main() -> None:
    """Run a health check from the command line."""
    parser = argparse.ArgumentParser(
        description="CloudPulse application health monitor"
    )
    parser.add_argument(
        "url",
        help="HTTP or HTTPS URL to monitor",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=3,
        help="Consecutive failures required to open an incident",
    )
    args = parser.parse_args()

    try:
        service = MonitoringService(
            service_url=args.url,
            failure_threshold=args.threshold,
        )
        result = service.run_check()

        print("Health Check Result:")
        print(json.dumps(result, indent=2))
        print(f"Stored checks: {service.history.count()}")

        for notification in service.notifications:
            print("\n" + notification)

        if not result["is_healthy"]:
            raise SystemExit(1)

    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
