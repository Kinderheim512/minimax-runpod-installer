"""Tests for the shared file-based recovery lock (launcher.infra_recover).

The lock serializes recovery actions across the MCP and GUI wirings. The
takeover rules are the safety property under test:

* a live holder's lock is never taken over;
* a dead holder's lock is taken over immediately;
* an *unparseable* lock (partial write / crash between create and write) is
  only taken over once provably stale — never from a fresh file, because the
  holder may still be writing its pid;
* release only removes the lock when it is still owned by this process (or
  is stale) — a loser of a takeover must not delete the new holder's lock.

All tests run against a lock in a tmp dir (the ``path`` parameter).
"""

import json
import os
import time

from launcher.infra_recover import FileRecoveryLock, _STALE_LOCK_SECONDS


def _dead_pid() -> int:
    """A pid that is verified dead on this system (windows pids span up to
    2^32-1, so 'impossible' is not a safe assumption)."""
    from launcher import runtime_state

    pid = 2**31 - 1
    while runtime_state.pid_is_alive(pid):
        pid -= 1
    return pid


def _age_file(path, seconds: float) -> None:
    past = time.time() - seconds
    os.utime(path, (past, past))


def test_acquire_creates_and_blocks_second_live_holder(tmp_path) -> None:
    lock = FileRecoveryLock(tmp_path / "recovery.lock")
    assert lock.acquire() is True
    # Same process re-acquiring: the recorded pid is this (live) process.
    assert lock.acquire() is False


def test_takeover_of_dead_holder_succeeds(tmp_path) -> None:
    path = tmp_path / "recovery.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": _dead_pid()}), encoding="utf-8")
    lock = FileRecoveryLock(path)
    assert lock.acquire() is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()


def test_takeover_of_live_holder_refused(tmp_path) -> None:
    path = tmp_path / "recovery.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    lock = FileRecoveryLock(path)
    assert lock.acquire() is False


def test_unparseable_fresh_lock_not_taken_over(tmp_path) -> None:
    """A fresh partial write may belong to a holder whose pid has not landed
    on disk yet: refusing to take it is the only safe choice."""
    path = tmp_path / "recovery.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")  # unparseable, mtime = now
    lock = FileRecoveryLock(path)
    assert lock.acquire() is False
    assert path.exists()


def test_unparseable_stale_lock_taken_over(tmp_path) -> None:
    path = tmp_path / "recovery.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("garbage-partial-write", encoding="utf-8")
    _age_file(path, _STALE_LOCK_SECONDS + 10)
    lock = FileRecoveryLock(path)
    assert lock.acquire() is True


def test_release_removes_own_lock(tmp_path) -> None:
    lock = FileRecoveryLock(tmp_path / "recovery.lock")
    lock.acquire()
    lock.release()
    assert not (tmp_path / "recovery.lock").exists()


def test_release_does_not_delete_foreign_fresh_lock(tmp_path) -> None:
    """After a takeover, the original holder's release() must not delete the
    *new* holder's lock."""
    path = tmp_path / "recovery.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": _dead_pid() + 7}), encoding="utf-8")
    lock = FileRecoveryLock(path)
    lock.release()
    assert path.exists()


def test_release_does_not_delete_fresh_unparseable_lock(tmp_path) -> None:
    path = tmp_path / "recovery.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    lock = FileRecoveryLock(path)
    lock.release()
    assert path.exists()


def test_release_deletes_stale_unparseable_lock(tmp_path) -> None:
    path = tmp_path / "recovery.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("partial", encoding="utf-8")
    _age_file(path, _STALE_LOCK_SECONDS + 10)
    lock = FileRecoveryLock(path)
    lock.release()
    assert not path.exists()


def test_release_missing_lock_is_noop(tmp_path) -> None:
    lock = FileRecoveryLock(tmp_path / "recovery.lock")
    lock.release()  # must not raise
