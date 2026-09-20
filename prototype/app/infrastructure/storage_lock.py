from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

try:  # pragma: no cover - platform-specific import
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - platform-specific import
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


class StorageLockError(RuntimeError):
    """The storage root is active or cannot be locked safely."""


class StorageLease:
    def __init__(self, *, path: Path, handle: BinaryIO) -> None:
        self.path = path
        self._handle = handle
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        try:
            if msvcrt is not None:  # pragma: no cover - exercised on Windows
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif fcntl is not None:  # pragma: no branch - exercised on POSIX
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._released = True
            self._handle.close()

    def __enter__(self) -> "StorageLease":
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def storage_lock_path(storage_root: Path) -> Path:
    root = Path(storage_root).expanduser().resolve(strict=False)
    return root.parent / f".{root.name}.reqpilotx.lock"


def acquire_storage_lease(
    storage_root: Path,
    *,
    blocking: bool = False,
) -> StorageLease:
    root = Path(storage_root).expanduser().resolve(strict=False)
    root.parent.mkdir(parents=True, exist_ok=True)
    lock_path = storage_lock_path(root)
    if lock_path.is_symlink():
        raise StorageLockError("Storage lock path must not be a symbolic link.")

    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
    except OSError as exc:
        raise StorageLockError("Storage lock file cannot be opened safely.") from exc

    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
        handle.seek(0)
        if msvcrt is not None:  # pragma: no cover - exercised on Windows
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            msvcrt.locking(handle.fileno(), mode, 1)
        elif fcntl is not None:  # pragma: no branch - exercised on POSIX
            mode = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), mode)
        else:  # pragma: no cover - unsupported standard-library platform
            raise StorageLockError("No standard-library storage lock is available.")
    except (OSError, StorageLockError) as exc:
        handle.close()
        if isinstance(exc, StorageLockError):
            raise
        raise StorageLockError(
            "Storage is active; close ReqPilotX before backup or restore."
        ) from exc
    return StorageLease(path=lock_path, handle=handle)
