"""The forward observation record cannot be reconstructed after it is lost.

U28: the tracker's default output IS its input ledger, and the old open("w")
truncated that file before the replacement was durable. These tests pin the
atomic writer and the writer lock.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The tracker refuses to import without a channel (it is operator-configured);
# a throwaway channel name exists only to get past the module-level guard.
_argv = sys.argv
try:
    sys.argv = [_argv[0], "--channel", "test-channel"]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from ops import call_tracker
finally:
    sys.argv = _argv

import fcntl

import pytest


def _point_ledger_at(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(call_tracker, "LEDGER", ledger)
    monkeypatch.setattr(call_tracker, "LEDGER_OUT", ledger)
    return ledger


def test_write_ledger_atomic_replaces_and_keeps_the_old_file_on_failure(tmp_path):
    dest = tmp_path / "ledger.jsonl"
    dest.write_text('{"id": "old"}\n')

    class _Boom(dict):
        def __getattribute__(self, name):
            raise RuntimeError("serializer exploded")

    with pytest.raises(RuntimeError):
        call_tracker.write_ledger_atomic(dest, [_Boom(id="new")])
    assert dest.read_text() == '{"id": "old"}\n', (
        "a failed write must leave the previous complete ledger intact"
    )
    assert list(tmp_path.glob(".calls-*")) == []

    call_tracker.write_ledger_atomic(dest, [{"id": "a"}, {"id": "b"}])
    assert dest.read_text() == '{"id": "a"}\n{"id": "b"}\n'
    assert list(tmp_path.glob(".calls-*")) == []


def test_a_second_writer_is_refused(tmp_path, monkeypatch):
    ledger = _point_ledger_at(tmp_path, monkeypatch)
    with open(str(ledger) + ".lock", "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        import asyncio

        with pytest.raises(RuntimeError, match="another call-tracker writer"):
            asyncio.run(call_tracker.run())
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
