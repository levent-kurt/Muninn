"""Tests for the worker's deterministic idle shutdown and its reaper.

This is the code that previously orphaned Chromium: the worker exits at idle
while a node driver and a browser tree are still alive, and Chromium detaches
its main browser into its *own* process group, so neither a group signal nor a
post-mortem pid walk can reach everything that needs killing.

These tests spawn real, harmless throwaway process trees so the reaper is
verified against real process semantics - groups, parents and all - rather than
a mock. Every spawned process is placed in its own session (``start_new_session``)
so that a group kill in a test can never reach the test runner itself. No real
browser is launched and every process is cleaned up afterwards.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress

import pytest

from browser_pool.worker import _reap, _tree_snapshot

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX process groups and signals"
)

SLEEPER = "import time; time.sleep(30)"
# Calls setsid, mimicking Chromium moving its browser into its own session.
DETACHED_SLEEPER = "import os, time; os.setsid(); time.sleep(30)"


def _spawn(code: str) -> subprocess.Popen[bytes]:
    """Start a throwaway process in its own session, so it leads its own group."""
    return subprocess.Popen([sys.executable, "-c", code], start_new_session=True)


def _sleep_tree(depth: int = 2) -> list[subprocess.Popen[bytes]]:
    """A small process tree, each member leading its own group."""
    procs = [_spawn(SLEEPER)]
    if depth >= 2:
        procs.append(_spawn(SLEEPER))
    if depth >= 3:
        procs.append(_spawn(DETACHED_SLEEPER))
    time.sleep(0.3)  # let parent/child links and groups settle
    return procs


def _alive(procs: list[subprocess.Popen[bytes]]) -> list[int]:
    return [p.pid for p in procs if p.poll() is None]


def _cleanup(procs: list[subprocess.Popen[bytes]]) -> None:
    for p in procs:
        if p.poll() is None:
            p.kill()
    for p in procs:
        with suppress(subprocess.TimeoutExpired):
            p.wait(timeout=5)


# --------------------------------------------------------------------------- snapshot


def test_tree_snapshot_walks_descendants_by_parentage() -> None:
    procs = _sleep_tree(depth=2)
    try:
        pairs, _profiles = _tree_snapshot(os.getpid())
        found = {pid for pid, _pgid in pairs}
        for p in procs:
            assert p.pid in found, f"pid {p.pid} missing from the snapshot"
    finally:
        _cleanup(procs)


def test_tree_snapshot_records_a_real_group_for_each_pid() -> None:
    procs = _sleep_tree(depth=2)
    try:
        pairs, _profiles = _tree_snapshot(os.getpid())
        for pid, pgid in pairs:
            assert pgid > 0
            # start_new_session=True makes each sleeper its own group leader.
            if pid in {p.pid for p in procs}:
                assert pgid == pid
    finally:
        _cleanup(procs)


def test_tree_snapshot_of_an_unknown_root_is_empty() -> None:
    pairs, profiles = _tree_snapshot(2**22 - 1)
    assert pairs == []
    assert profiles == set()


def test_tree_snapshot_finds_no_profile_in_plain_sleepers() -> None:
    procs = _sleep_tree(depth=1)
    try:
        _pairs, profiles = _tree_snapshot(os.getpid())
        assert all("playwright" not in p for p in profiles)
    finally:
        _cleanup(procs)


# --------------------------------------------------------------------------- reaper


def test_reap_kills_every_pid_in_the_snapshot() -> None:
    procs = _sleep_tree(depth=2)
    try:
        assert len(_alive(procs)) == len(procs)
        tree = [(p.pid, os.getpgid(p.pid)) for p in procs]
        _reap(tree, set())
        time.sleep(0.5)
        assert _alive(procs) == [], "reaper left processes behind"
    finally:
        _cleanup(procs)


def _fork_pair(child_body: str = "") -> tuple[subprocess.Popen[bytes], int, int]:
    """Spawn a process that forks a child, then report both pids.

    The child inherits the parent's process group unless ``child_body`` makes it
    call ``setsid()`` first, which is how we reproduce a Chromium browser that
    detaches into its own group. The group is created by ``start_new_session``
    rather than ``preexec_fn=setpgrp``, which the sandbox forbids.
    """
    program = (
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        + "".join(f"    {line}\n" for line in (child_body or "pass").splitlines())
        + "    time.sleep(30)\n"
        "    os._exit(0)\n"
        "print(os.getpid(), pid, flush=True)\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", program],
        start_new_session=True,
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().decode().strip()
    parent_pid, child_pid = (int(part) for part in line.split())
    time.sleep(0.3)  # let the child reach its sleep
    return proc, parent_pid, child_pid


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - still exists, just not ours
        return True
    return True


def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    """Poll until ``pid`` is really gone.

    A killed direct child stays in the table as a zombie until the parent
    reaps it, and ``kill(pid, 0)`` succeeds for a zombie - so callers must
    ``wait()`` their own child and only poll for the grandchildren, which
    launchd reaps for us.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_reap_kills_a_group_member_it_never_listed() -> None:
    """A process born after the snapshot is still caught by its group.

    This is the exact case a pid-only reaper misses: the pid did not exist when
    the tree was captured, so only the recorded process group can reach it.
    """
    proc, parent_pid, child_pid = _fork_pair()
    try:
        group = os.getpgid(parent_pid)
        assert os.getpgid(child_pid) == group, "child should share the parent's group"

        # The snapshot lists the parent only; the child is the "late arrival".
        _reap([(parent_pid, group)], set())
        proc.wait(timeout=5)
        assert proc.returncode is not None, "listed pid survived"
        assert _wait_gone(child_pid), "unlisted group member survived"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_reap_reaches_a_process_that_detached_into_its_own_group() -> None:
    """Chromium's browser leaves the worker's group; the pid pass still gets it.

    The child calls ``setsid()`` first, so it is in neither the worker's group
    nor its parent's. Only the per-pid pass of the snapshot can reach it.
    """
    proc, parent_pid, child_pid = _fork_pair(child_body="os.setsid()")
    try:
        assert os.getpgid(child_pid) != os.getpgid(parent_pid), "child should have detached"

        # Both are listed, because the snapshot (taken before teardown) saw
        # both; the group signal alone would have missed the detached child.
        _reap(
            [
                (parent_pid, os.getpgid(parent_pid)),
                (child_pid, os.getpgid(child_pid)),
            ],
            set(),
        )
        proc.wait(timeout=5)
        assert proc.returncode is not None
        assert _wait_gone(child_pid), "detached process survived the reaper"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_reap_is_harmless_for_dead_and_sentinel_entries() -> None:
    procs = _sleep_tree(depth=1)
    tree = [(p.pid, os.getpgid(p.pid)) for p in procs]
    _cleanup(procs)
    _reap(tree, set())  # already dead: must not raise
    _reap([], set())
    _reap([(0, 0)], set())  # sentinels skipped, never used as "our own group"


def test_reap_ignores_an_unrelated_profile_directory() -> None:
    procs = _sleep_tree(depth=1)
    try:
        _reap([], {"/nonexistent/profile/dir-xyz"})
        assert len(_alive(procs)) == len(procs), "unrelated profile must kill nothing"
    finally:
        _cleanup(procs)


# --------------------------------------------------------------------------- group model


def test_reaper_child_survives_the_kill_of_the_group_it_left() -> None:
    """A reaper that called setsid() is not hit by the worker's group kill.

    Reproduces the shutdown sequence in miniature inside a throwaway session:
    a child leaves its group and keeps running, then the group it left is
    SIGKILLed, and the child still reports in.
    """
    program = (
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()          # leave the worker's group\n"
        "    time.sleep(0.3)\n"
        "    os.write(1, b'alive')\n"
        "    os._exit(0)\n"
        "os.waitpid(pid, 0)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", program],
        start_new_session=True,
        stdout=subprocess.PIPE,
    )
    try:
        group = os.getpgid(proc.pid)
        out, _ = proc.communicate(timeout=15)
        # The group is now empty (its only member exited), and the detached
        # grandchild was never a member of it.
        assert out.strip() == b"alive"
        assert group == proc.pid
    finally:
        if proc.poll() is None:  # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=5)


def test_setpgid_makes_a_process_its_own_group_leader() -> None:
    """The guarantee main() gives the worker before it starts serving."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import os; os.setpgid(0, 0); print(os.getpgid(0))"],
        stdout=subprocess.PIPE,
    )
    out, _ = proc.communicate(timeout=15)
    assert int(out.strip()) == proc.pid


def test_killpg_reaches_only_its_own_group() -> None:
    """Sanity check on the primitive the whole design depends on."""
    inside = _spawn(SLEEPER)
    outside = _spawn(SLEEPER)
    time.sleep(0.3)
    try:
        assert os.getpgid(inside.pid) != os.getpgid(outside.pid)
        os.killpg(os.getpgid(inside.pid), signal.SIGKILL)
        time.sleep(0.4)
        assert inside.poll() is not None, "killpg did not reach its member"
        assert outside.poll() is None, "killpg reached outside its group"
    finally:
        _cleanup([inside, outside])


def test_reap_never_signals_the_protected_process_group() -> None:
    """The killpg pass must skip the group it was told to protect.

    Regression: the reaper used to compare against its *own* pgid, but it calls
    setsid() before working, so it no longer shares the worker's group and the
    comparison never matched - the reaper then SIGKILLed the worker itself.
    The worker's pid and group are therefore passed in explicitly.
    """
    own_pid, own_pgid = os.getpid(), os.getpgid(os.getpid())
    # A pid that cannot exist, in the group we must not touch: only the group
    # pass could do harm here.
    _reap([(2**22 - 1, own_pgid)], set(), protect=(own_pid, own_pgid))
    time.sleep(0.2)
    assert os.getpid() > 0, "the reaper signalled the process group it was told to protect"


def test_reap_still_kills_a_group_it_was_not_told_to_protect() -> None:
    """The guard must exempt one group, not disable the group pass."""
    procs = _sleep_tree(depth=1)
    try:
        group = os.getpgid(procs[0].pid)
        _reap([(2**22 - 1, group)], set(), protect=(os.getpid(), os.getpgid(0)))
        # wait() reaps: a killed child stays in the table as a zombie until its
        # parent collects it, and os.kill(pid, 0) still succeeds on a zombie.
        procs[0].wait(timeout=5)
        assert procs[0].returncode is not None, "an unprotected group was not swept"
    finally:
        _cleanup(procs)


def test_reap_kills_processes_that_share_the_protected_group_by_pid() -> None:
    """A descendant in the worker's own group still dies - by pid, not by group.

    Group-killing the worker's group is unsafe, so the pid pass is what
    guarantees cleanup for anything that shares it.
    """
    # No start_new_session: this child inherits our process group.
    sibling = subprocess.Popen([sys.executable, "-c", SLEEPER])
    time.sleep(0.3)
    try:
        own_pgid = os.getpgid(os.getpid())
        assert os.getpgid(sibling.pid) == own_pgid
        _reap([(sibling.pid, own_pgid)], set(), protect=(os.getpid(), own_pgid))
        sibling.wait(timeout=5)
        assert sibling.poll() is not None, "a listed process in the protected group survived"
    finally:
        _cleanup([sibling])
