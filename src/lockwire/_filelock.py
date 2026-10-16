"""Blocking exclusive lock on an open fd, POSIX and Windows, stdlib only."""

from __future__ import annotations

import os
import time


def lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        deadline = time.monotonic() + 30
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("lock held by another lockwire process") from None
                time.sleep(0.1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)


def unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
