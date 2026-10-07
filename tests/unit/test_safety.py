import time

import pytest

from calendar_sync.config import SafetyConfig
from calendar_sync.safety import (
    DeletionLimitExceededError,
    LockHeldError,
    ProcessLock,
    RuntimeBudget,
    RuntimeBudgetExceededError,
    check_deletion_limits,
)


def default_safety(**overrides):
    base = dict(
        dry_run_default=True, max_deletes_absolute=5, max_delete_ratio=0.25,
        max_runtime_seconds=60, require_all_sources=True,
    )
    base.update(overrides)
    return SafetyConfig(**base)


def test_lock_blocks_concurrent_acquire(tmp_path):
    lock_path = tmp_path / "run" / "calendar-sync.lock"
    lock1 = ProcessLock(lock_path)
    lock1.acquire()
    try:
        lock2 = ProcessLock(lock_path)
        with pytest.raises(LockHeldError):
            lock2.acquire()
    finally:
        lock1.release()


def test_lock_can_be_reacquired_after_release(tmp_path):
    lock_path = tmp_path / "run" / "calendar-sync.lock"
    lock1 = ProcessLock(lock_path)
    lock1.acquire()
    lock1.release()
    lock2 = ProcessLock(lock_path)
    lock2.acquire()
    lock2.release()


def test_lock_context_manager(tmp_path):
    lock_path = tmp_path / "run" / "calendar-sync.lock"
    with ProcessLock(lock_path):
        with pytest.raises(LockHeldError):
            ProcessLock(lock_path).acquire()


def test_runtime_budget_not_expired_initially():
    budget = RuntimeBudget(60)
    assert not budget.expired()
    budget.ensure_not_expired()


def test_runtime_budget_expires():
    budget = RuntimeBudget(0.05)
    time.sleep(0.1)
    assert budget.expired()
    with pytest.raises(RuntimeBudgetExceededError):
        budget.ensure_not_expired()


def test_deletion_limit_absolute():
    safety = default_safety(max_deletes_absolute=5, max_delete_ratio=1.0)
    with pytest.raises(DeletionLimitExceededError):
        check_deletion_limits(10, baseline_active_mappings=100, safety=safety, allow_large_delete=False)


def test_deletion_limit_ratio():
    safety = default_safety(max_deletes_absolute=1000, max_delete_ratio=0.25)
    with pytest.raises(DeletionLimitExceededError):
        check_deletion_limits(3, baseline_active_mappings=8, safety=safety, allow_large_delete=False)


def test_deletion_within_limits_passes():
    safety = default_safety(max_deletes_absolute=50, max_delete_ratio=0.25)
    check_deletion_limits(2, baseline_active_mappings=100, safety=safety, allow_large_delete=False)


def test_zero_deletes_always_passes_even_with_zero_baseline():
    safety = default_safety(max_deletes_absolute=0, max_delete_ratio=0.0)
    check_deletion_limits(0, baseline_active_mappings=0, safety=safety, allow_large_delete=False)


def test_empty_desired_set_alone_is_not_sufficient_justification():
    """An empty desired set producing a mass-delete plan must still be
    blocked by the same guard - the guard has no special case for 'why'."""
    safety = default_safety(max_deletes_absolute=1000, max_delete_ratio=0.1)
    with pytest.raises(DeletionLimitExceededError):
        check_deletion_limits(50, baseline_active_mappings=100, safety=safety, allow_large_delete=False)


def test_allow_large_delete_overrides_both_limits():
    safety = default_safety(max_deletes_absolute=1, max_delete_ratio=0.01)
    check_deletion_limits(99, baseline_active_mappings=100, safety=safety, allow_large_delete=True)
