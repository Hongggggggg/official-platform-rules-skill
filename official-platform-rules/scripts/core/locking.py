from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import utc_now


class SyncLocked(RuntimeError):
    pass


@contextmanager
def platform_sync_lock(
    data_root: Path,
    *,
    stale_after_seconds: int = 3600,
) -> Iterator[Path]:
    """Prevent concurrent writers for one platform database.

    The lock is deliberately platform-local. A stale lock is removed only
    after its age exceeds the configured safety window.
    """

    lock_path = data_root / ".sync.lock"
    data_root.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        age = time.time() - lock_path.stat().st_mtime
        if age > stale_after_seconds:
            lock_path.unlink()
    payload = json.dumps(
        {"pid": os.getpid(), "created_at": utc_now()},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError as exc:
        raise SyncLocked(
            f"平台同步正在运行，锁文件为 {lock_path.name}"
        ) from exc
    try:
        os.write(descriptor, payload)
        os.close(descriptor)
        descriptor = -1
        yield lock_path
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
