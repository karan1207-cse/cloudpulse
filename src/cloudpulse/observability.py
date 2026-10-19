"""Structured JSON logging and CloudWatch Embedded Metric Format (EMF)."""

import json
import logging
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, TextIO

METRIC_NAMESPACE = "CloudPulse"
_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Format log records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", *, json_format: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    handler = logging.StreamHandler(sys.stderr)
    if json_format:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.handlers = [handler]
    # Keep SDK/HTTP internals (which can include request details) out of logs.
    for noisy in ("boto3", "botocore", "urllib3"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))


def emf_record(
    metrics: Mapping[str, int | float],
    *,
    dimensions: Mapping[str, str] | None = None,
    namespace: str = METRIC_NAMESPACE,
    timestamp_ms: int | None = None,
) -> dict[str, Any]:
    """Build an EMF document; CloudWatch extracts metrics from the log line.

    EMF needs no ``cloudwatch:PutMetricData`` permission and no extra API
    calls. Only fixed, aggregate dimensions are used so the number of custom
    metrics (and their cost) does not grow with the number of services.
    """
    dims = dict(dimensions or {})
    timestamp = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    return {
        "_aws": {
            "Timestamp": timestamp,
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [sorted(dims)],
                    "Metrics": [{"Name": name, "Unit": "Count"} for name in metrics],
                }
            ],
        },
        **dims,
        **dict(metrics),
    }


def emit_metrics(
    metrics: Mapping[str, int | float],
    *,
    dimensions: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
) -> dict[str, Any]:
    document = emf_record(metrics, dimensions=dimensions)
    print(json.dumps(document), file=stream or sys.stdout, flush=True)
    return document
