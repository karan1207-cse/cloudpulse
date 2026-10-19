"""DynamoDB storage adapter for CloudPulse health checks.

Table schema (see ``infra/terraform/dynamodb.tf``)::

    PK  service (S)   service name
    SK  sk      (S)   "<checked_at ISO-8601 UTC>#<check id>"  (sortable by time)
    TTL expires_at (N) epoch seconds; DynamoDB deletes expired history for free

Per-service history is a single ``Query`` (newest first). ``get_all`` and
``count`` scan the table and are administrative only — no monitoring or API
path uses them.
"""

from datetime import timedelta
from typing import Any
from uuid import uuid4

from cloudpulse.aws_common import aws_call, dynamodb_resource, paginate, to_dynamo
from cloudpulse.clock import Clock, parse_utc_iso, to_utc_iso, utc_now


class DynamoDBHealthCheckStorage:
    """Persist health-check records in a DynamoDB table."""

    def __init__(
        self,
        table_name: str,
        region_name: str | None = None,
        table: Any = None,
        retention_days: int | None = 30,
        clock: Clock = utc_now,
    ) -> None:
        """Initialize storage using a table name and optional AWS region."""
        if not table_name.strip():
            raise ValueError("table_name cannot be empty.")
        if retention_days is not None and retention_days < 1:
            raise ValueError("retention_days must be at least 1.")
        self.table_name = table_name
        self.retention_days = retention_days
        self.clock = clock
        self.table = (
            table
            if table is not None
            else (dynamodb_resource(region_name).Table(table_name))
        )

    def add(self, result: dict[str, Any]) -> None:
        """Store a health-check record."""
        item = result.copy()
        item.setdefault("id", str(uuid4()))
        item.setdefault("checked_at", to_utc_iso(self.clock()))
        item["service"] = item.get("service") or item.get("url") or "unknown"
        item["sk"] = f"{item['checked_at']}#{item['id']}"
        if self.retention_days is not None:
            checked = parse_utc_iso(item["checked_at"])
            item["expires_at"] = int(
                (checked + timedelta(days=self.retention_days)).timestamp()
            )
        aws_call(
            "Saving health check",
            self.table.put_item,
            Item=to_dynamo(item),
        )

    def get_recent(self, service: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return the newest records for one service (single Query)."""
        if limit < 1:
            return []
        from boto3.dynamodb.conditions import Key

        return paginate(
            "Querying health checks",
            self.table.query,
            limit=limit,
            KeyConditionExpression=Key("service").eq(service),
            ScanIndexForward=False,
        )

    def get_all(self) -> list[dict[str, Any]]:
        """Return all stored health-check records (paginated scan; admin only)."""
        return paginate("Scanning health checks", self.table.scan)

    def count(self) -> int:
        """Return the number of stored health-check records (admin only)."""
        total = 0
        request: dict[str, Any] = {"Select": "COUNT"}
        while True:
            response = aws_call("Counting health checks", self.table.scan, **request)
            total += int(response.get("Count", len(response.get("Items", []))))
            if "LastEvaluatedKey" not in response:
                return total
            request["ExclusiveStartKey"] = response["LastEvaluatedKey"]
