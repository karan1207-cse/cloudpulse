
"""Command-line interface for CloudPulse."""

import argparse
import json
import time

from cloudpulse.monitor import MonitoringService


def main() -> None:
    """Continuously monitor an HTTP endpoint."""
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
    parser.add_argument(
        "--interval",
        type=float,
        default=10.0,
        help="Seconds to wait between health checks",
    )

    args = parser.parse_args()

    if args.interval <= 0:
        parser.error("--interval must be greater than zero")

    if args.threshold < 1:
        parser.error("--threshold must be at least 1")

    try:
        service = MonitoringService(
            service_url=args.url,
            failure_threshold=args.threshold,
        )

        print(f"Monitoring: {args.url}")
        print(f"Check interval: {args.interval} seconds")
        print("Press Ctrl+C to stop.\n")

        check_number = 0

        while True:
            check_number += 1
            result = service.run_check()

            print(f"\n--- Health Check #{check_number} ---")
            print(json.dumps(result, indent=2))
            print(f"Stored checks: {service.history.count()}")

            for notification in service.notifications:
                print(notification)

            service.notifications.clear()

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nCloudPulse monitoring stopped.")

    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
