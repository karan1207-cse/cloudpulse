"""Small helpers for safe JSON file persistence."""

import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cloudpulse.errors import StorageCorruptError, StorageError


def ensure_file(path: Path, default: str) -> None:
    """Create ``path`` (and parents) with ``default`` content if missing."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            atomic_write_text(path, default)
    except OSError as exc:
        raise StorageError(f"Cannot initialise storage file {path}: {exc}") from exc


def read_json(path: Path, expected: type, default: Any) -> Any:
    """Read JSON of type ``expected``; missing file returns ``default``.

    A corrupt file raises :class:`StorageCorruptError` and is left untouched
    so no user data is destroyed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except OSError as exc:
        raise StorageError(f"Cannot read {path}: {exc}") from exc
    if not text.strip():
        return default
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StorageCorruptError(
            f"{path} is not valid JSON (line {exc.lineno}). "
            "Fix or move the file; CloudPulse will not overwrite it."
        ) from exc
    if not isinstance(data, expected):
        kind = "list" if expected is list else "object"
        raise StorageCorruptError(f"{path} must contain a JSON {kind}.")
    return data


def _replace_with_retry(source: str, target: Path, attempts: int = 40) -> None:
    """``os.replace`` that tolerates brief sharing violations on Windows.

    On Windows, replacing a file fails with ``PermissionError`` while another
    thread or process has it open (e.g. the API reading it while the
    scheduler writes). The lock is held only for the duration of a read, so
    retrying for up to ~2 seconds is enough.
    """
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` atomically: temp file in the same directory + replace."""
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise StorageError(f"Cannot write {path}: {exc}") from exc


def write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, default=str))


@contextmanager
def file_mutex(
    path: Path, timeout: float = 5.0, stale_after: float = 30.0
) -> Iterator[None]:
    """Cross-process mutex using an exclusively created lock file.

    A lock file older than ``stale_after`` seconds (left by a crashed
    process) is removed.
    """
    lock = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale_after:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise StorageError(f"Timed out waiting for lock {lock}") from None
            time.sleep(0.05)
        except OSError as exc:
            raise StorageError(f"Cannot create lock {lock}: {exc}") from exc
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)
