"""Exception types shared across CloudPulse."""


class CloudPulseError(Exception):
    """Base class for all CloudPulse errors."""


class ConfigError(CloudPulseError):
    """Configuration is missing or invalid."""


class UnsafeURLError(CloudPulseError, ValueError):
    """A target URL is malformed or points at a blocked destination."""


class StorageError(CloudPulseError):
    """Storage is unavailable or an operation failed."""


class StorageCorruptError(StorageError):
    """Stored data exists but cannot be parsed or has the wrong shape."""


class ConcurrencyConflictError(StorageError):
    """A conditional write lost a race with another writer."""


class NotificationError(CloudPulseError):
    """A notification could not be delivered."""


class ServiceValidationError(ConfigError):
    """A service definition is invalid; ``field`` names the offending input."""

    def __init__(self, field: str | None, detail: str, where: str | None = None):
        self.field = field
        self.detail = detail
        prefix = ".".join(part for part in (where, field) if part)
        super().__init__(f"{prefix}: {detail}" if prefix else detail)


class ServiceNotFoundError(CloudPulseError):
    """No service with the given name exists."""


class ServiceReadOnlyError(CloudPulseError):
    """The service is defined in configuration and cannot be changed via the API."""
