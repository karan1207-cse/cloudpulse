"""Shared helpers for AWS adapters (boto3 is imported lazily)."""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from cloudpulse.errors import StorageError

try:  # boto3/botocore are optional for local-only use.
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover - exercised only without boto3 installed

    class BotoCoreError(Exception):  # type: ignore[no-redef]
        """Placeholder when botocore is not installed."""

    class ClientError(Exception):  # type: ignore[no-redef]
        """Placeholder when botocore is not installed."""

        response: dict[str, Any]


def require_boto3() -> Any:
    try:
        import boto3
    except ImportError as exc:
        raise StorageError(
            "boto3 is required for the AWS backend: pip install boto3"
        ) from exc
    return boto3


def error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None) or {}
    return str(response.get("Error", {}).get("Code", type(exc).__name__))


def aws_call[T](
    description: str, func: Callable[..., T], *args: Any, **kwargs: Any
) -> T:
    """Call an AWS API, converting SDK errors into :class:`StorageError`."""
    try:
        return func(*args, **kwargs)
    except ClientError as exc:
        raise StorageError(f"{description} failed: {error_code(exc)}") from exc
    except BotoCoreError as exc:
        raise StorageError(f"{description} failed: {type(exc).__name__}") from exc


def dynamodb_resource(region_name: str | None = None) -> Any:
    """Create a DynamoDB resource using the standard credential chain."""
    boto3 = require_boto3()
    try:
        return boto3.resource("dynamodb", region_name=region_name)
    except BotoCoreError as exc:
        raise StorageError(
            f"Cannot create DynamoDB client ({type(exc).__name__}). "
            "Set AWS_REGION and configure credentials via the standard chain."
        ) from exc


def to_dynamo(value: Any) -> Any:
    """Convert Python values to DynamoDB-safe types (floats -> Decimal)."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {str(k): to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_dynamo(v) for v in value]
    return value


def from_dynamo(value: Any) -> Any:
    """Convert DynamoDB values back to plain Python (Decimal -> int/float)."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    if isinstance(value, set):
        return [from_dynamo(v) for v in sorted(value)]
    return value


def paginate(
    description: str,
    operation: Callable[..., dict[str, Any]],
    limit: int | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Run a Query/Scan following ``LastEvaluatedKey`` until ``limit`` items."""
    items: list[dict[str, Any]] = []
    request = dict(kwargs)
    while True:
        if limit is not None:
            request["Limit"] = max(1, min(limit - len(items), 1000))
        response = aws_call(description, operation, **request)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key or (limit is not None and len(items) >= limit):
            break
        request["ExclusiveStartKey"] = last_key
    if limit is not None:
        items = items[:limit]
    return [from_dynamo(item) for item in items]
