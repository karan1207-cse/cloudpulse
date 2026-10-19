"""DynamoDB storage adapter for CloudPulse incidents and monitoring state.

Incidents table (see ``infra/terraform/dynamodb.tf``)::

    PK  id (S)
    GSI service-created-index  PK service_name (S)  SK created_at (S)
    GSI status-created-index   PK status (S)        SK created_at (S)

Monitoring-state table (separate, so incident counts can never include
internal state records)::

    PK  service (S)    attributes: consecutive_failures, active_incident_id,
                       last_* fields, version (N, optimistic concurrency)

Writes are conditional: incidents are created with ``attribute_not_exists``
and updated with ``UpdateItem`` + ``attribute_exists`` (no read-modify-write);
state saves require the stored ``version`` to match.
"""

from typing import Any

from cloudpulse.aws_common import (
    ClientError,
    aws_call,
    dynamodb_resource,
    error_code,
    from_dynamo,
    paginate,
    to_dynamo,
)
from cloudpulse.errors import ConcurrencyConflictError, StorageError
from cloudpulse.incident_manager import Incident, incident_from_dict
from cloudpulse.state import ServiceState

SERVICE_INDEX = "service-created-index"
STATUS_INDEX = "status-created-index"
_IMMUTABLE_FIELDS = {"id"}


class DynamoDBIncidentStorage:
    """Persist incidents (and, optionally, monitoring state) in DynamoDB."""

    def __init__(
        self,
        table_name: str,
        region_name: str | None = None,
        table: Any = None,
        state_table_name: str | None = None,
        state_table: Any = None,
    ) -> None:
        """Initialize incident storage."""
        if not table_name.strip():
            raise ValueError("table_name cannot be empty.")
        if state_table_name is not None and not state_table_name.strip():
            raise ValueError("state_table_name cannot be empty.")

        self.table_name = table_name
        self.state_table_name = state_table_name
        resource = None
        if table is None or (state_table is None and state_table_name):
            resource = dynamodb_resource(region_name)
        self.table = table if table is not None else resource.Table(table_name)
        if state_table is not None:
            self.state_table = state_table
        elif state_table_name:
            self.state_table = resource.Table(state_table_name)
        else:
            self.state_table = None

    # -- incidents ----------------------------------------------------------

    def add(self, incident: dict[str, Any]) -> None:
        """Store a new incident; fail if the id already exists."""
        if "id" not in incident:
            raise ValueError("Incident must contain an id.")
        try:
            self.table.put_item(
                Item=to_dynamo(incident.copy()),
                ConditionExpression="attribute_not_exists(id)",
            )
        except ClientError as exc:
            if error_code(exc) == "ConditionalCheckFailedException":
                raise ConcurrencyConflictError(
                    f"Incident {incident['id']} already exists."
                ) from exc
            raise StorageError(f"Saving incident failed: {error_code(exc)}") from exc

    def get(self, incident_id: str) -> dict[str, Any] | None:
        response = aws_call(
            "Reading incident", self.table.get_item, Key={"id": incident_id}
        )
        item = response.get("Item")
        return from_dynamo(item) if item is not None else None

    def get_all(self) -> list[dict[str, Any]]:
        """Return all incidents via a paginated scan (administrative only)."""
        return paginate("Scanning incidents", self.table.scan)

    def update(self, incident_id: str, updates: dict[str, Any]) -> bool:
        """Atomically update an incident if it exists (single ``UpdateItem``)."""
        fields = {k: v for k, v in updates.items() if k not in _IMMUTABLE_FIELDS}
        if not fields:
            return self.get(incident_id) is not None
        names = {f"#f{i}": key for i, key in enumerate(fields)}
        values = {f":v{i}": to_dynamo(value) for i, value in enumerate(fields.values())}
        expression = "SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields)))
        try:
            self.table.update_item(
                Key={"id": incident_id},
                UpdateExpression=expression,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(id)",
            )
        except ClientError as exc:
            if error_code(exc) == "ConditionalCheckFailedException":
                return False
            raise StorageError(f"Updating incident failed: {error_code(exc)}") from exc
        return True

    def list_incidents(
        self,
        *,
        status: str | None = None,
        service: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return incidents newest first using the GSIs (never a scan)."""
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer.")
        from boto3.dynamodb.conditions import Attr, Key

        if service is not None:
            kwargs: dict[str, Any] = {
                "IndexName": SERVICE_INDEX,
                "KeyConditionExpression": Key("service_name").eq(service),
                "ScanIndexForward": False,
            }
            if status is not None:
                kwargs["FilterExpression"] = Attr("status").eq(status)
                # A filter is applied after Limit, so page until enough match.
                items = paginate("Querying incidents", self.table.query, **kwargs)
                return items[:limit]
            return paginate(
                "Querying incidents", self.table.query, limit=limit, **kwargs
            )

        statuses = [status] if status is not None else ["OPEN", "RECOVERED"]
        merged: list[dict[str, Any]] = []
        for value in statuses:
            merged.extend(
                paginate(
                    "Querying incidents",
                    self.table.query,
                    limit=limit,
                    IndexName=STATUS_INDEX,
                    KeyConditionExpression=Key("status").eq(value),
                    ScanIndexForward=False,
                )
            )
        merged.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return merged[:limit]

    def get_open_incidents(self, service: str) -> list[Incident]:
        return [
            incident_from_dict(record)
            for record in self.list_incidents(
                status="OPEN", service=service, limit=1000
            )
        ]

    def get_open_incident(self, service_url: str) -> Incident | None:
        """Return the newest open incident for a service as an ``Incident``."""
        incidents = self.get_open_incidents(service_url)
        return incidents[0] if incidents else None

    def count(self) -> int:
        """Count incidents (paginated ``Select=COUNT`` scan; admin only).

        Monitoring state lives in its own table, so it is never counted.
        """
        total = 0
        request: dict[str, Any] = {"Select": "COUNT"}
        while True:
            response = aws_call("Counting incidents", self.table.scan, **request)
            total += int(response.get("Count", len(response.get("Items", []))))
            if "LastEvaluatedKey" not in response:
                return total
            request["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    # -- monitoring state ---------------------------------------------------

    def _require_state_table(self) -> Any:
        if self.state_table is None:
            raise StorageError(
                "Monitoring-state table is not configured (set CLOUDPULSE_STATE_TABLE)."
            )
        return self.state_table

    def get_state(self, service: str) -> ServiceState | None:
        table = self._require_state_table()
        response = aws_call(
            "Reading monitoring state",
            table.get_item,
            Key={"service": service},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if item is None:
            return None
        return ServiceState.from_dict(from_dynamo(item))

    def save_state(self, state: ServiceState) -> ServiceState:
        table = self._require_state_table()
        saved = state.with_changes(version=state.version + 1)
        item = saved.to_dict()
        condition: dict[str, Any]
        if state.version == 0:
            condition = {
                "ConditionExpression": "attribute_not_exists(#s)",
                "ExpressionAttributeNames": {"#s": "service"},
            }
        else:
            condition = {
                "ConditionExpression": "#v = :expected",
                "ExpressionAttributeNames": {"#v": "version"},
                "ExpressionAttributeValues": {":expected": state.version},
            }
        try:
            table.put_item(Item=to_dynamo(item), **condition)
        except ClientError as exc:
            if error_code(exc) == "ConditionalCheckFailedException":
                raise ConcurrencyConflictError(
                    f"State for {state.service!r} was modified concurrently."
                ) from exc
            raise StorageError(
                f"Saving monitoring state failed: {error_code(exc)}"
            ) from exc
        return saved

    def get_failure_count(self, service_url: str) -> int:
        state = self.get_state(service_url)
        return state.consecutive_failures if state else 0

    def save_failure_count(self, service_url: str, count: int) -> None:
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("Failure count must be a non-negative integer.")
        state = self.get_state(service_url) or ServiceState(service=service_url)
        self.save_state(state.with_changes(consecutive_failures=count))
