"""Process lock, runtime budget, and deletion-limit guards.

These are the "fail closed" primitives from
TECHNICAL_SPECIFICATION.md section 2.3/11.2: an overlapping run, a run
that overshoots its hosting time limit, or a plan that would delete an
implausibly large share of managed events must all stop the run before
any target mutation, not clean up after the fact.
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from pathlib import Path

from .config import SafetyConfig


class LockHeldError(RuntimeError):
    """Another instance of this process already holds the run lock."""


class RuntimeBudgetExceededError(RuntimeError):
    """The configured max_runtime_seconds was exceeded before completion."""


class DeletionLimitExceededError(RuntimeError):
    """The planned deletes exceed the configured absolute/ratio limits."""

    def __init__(self, message: str, *, planned_deletes: int, baseline: int):
        super().__init__(message)
        self.planned_deletes = planned_deletes
        self.baseline = baseline


class ProcessLock:
    """A non-blocking exclusive file lock (NFR-005). The lock file itself
    is never deleted - only its content (the holder's PID, informational
    only) is rewritten - so lock ownership is entirely determined by the
    OS-level flock, not by file existence."""

    def __init__(self, path: Path):
        self._path = path
        self._fh = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise LockHeldError(
                    f"Another run holds the lock at {self._path}; refusing to start concurrently."
                ) from exc
            raise
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class RuntimeBudget:
    """Tracks elapsed time against safety.max_runtime_seconds so the
    reconciler can stop starting new writes before a shared-hosting
    process time limit kills the run mid-write."""

    def __init__(self, max_runtime_seconds: int, *, clock=time.monotonic):
        self._deadline = clock() + max_runtime_seconds
        self._clock = clock

    def remaining_seconds(self) -> float:
        return self._deadline - self._clock()

    def expired(self) -> bool:
        return self.remaining_seconds() <= 0

    def ensure_not_expired(self) -> None:
        if self.expired():
            raise RuntimeBudgetExceededError(
                "Runtime budget exceeded; stopping before starting further mutations."
            )


def check_deletion_limits(
    planned_deletes: int,
    *,
    baseline_active_mappings: int,
    safety: SafetyConfig,
    allow_large_delete: bool,
) -> None:
    """Raise DeletionLimitExceededError if `planned_deletes` exceeds the
    configured absolute count or ratio of previously-active managed
    mappings, unless the operator passed --allow-large-delete for this
    one run. An empty desired set alone is never sufficient justification
    for mass deletion - this guard applies regardless of *why* the
    deletes were planned (NFR-007)."""
    if allow_large_delete or planned_deletes == 0:
        return

    if planned_deletes > safety.max_deletes_absolute:
        raise DeletionLimitExceededError(
            f"Planned deletes ({planned_deletes}) exceed safety.max_deletes_absolute "
            f"({safety.max_deletes_absolute}). Re-run with --allow-large-delete to override once.",
            planned_deletes=planned_deletes,
            baseline=baseline_active_mappings,
        )

    if baseline_active_mappings > 0:
        ratio = planned_deletes / baseline_active_mappings
        if ratio > safety.max_delete_ratio:
            raise DeletionLimitExceededError(
                f"Planned deletes ({planned_deletes}) are {ratio:.0%} of {baseline_active_mappings} "
                f"previously-active managed mappings, exceeding safety.max_delete_ratio "
                f"({safety.max_delete_ratio:.0%}). Re-run with --allow-large-delete to override once.",
                planned_deletes=planned_deletes,
                baseline=baseline_active_mappings,
            )
