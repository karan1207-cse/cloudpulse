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
