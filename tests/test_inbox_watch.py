from multiprocessing import get_context
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from loop_reviewer.inbox.model import InboxReport, InboxSettings, JiraProjectKey
from loop_reviewer.integrations.local_state.inbox_watch import InboxWatch

_REPORT = InboxReport(
    InboxSettings((JiraProjectKey("ABC"),), (), version=1, scan_version=1), (), ()
)


def test_watchers_share_deadline_and_manual_scan_satisfies_due_tick(
    tmp_path: Path,
) -> None:
    now = 0.0
    starts: list[float] = []

    def scan() -> InboxReport:
        nonlocal now
        starts.append(now)
        if now == 110:
            now = 130
        return _REPORT

    first = InboxWatch(tmp_path, scan, monotonic=lambda: now)
    second = InboxWatch(tmp_path, scan, monotonic=lambda: now)
    with first.join():
        first.tick()
        with second.join():
            second.tick()
            assert starts == [0]
            assert second.cycle() == first.cycle()
            now = 110
            assert second.scan_now() == _REPORT
            first.tick()
            assert starts == [0, 110]
        now = 240
        first.tick()
        assert starts == [0, 110, 240]
    now = 360
    second.tick()
    assert starts == [0, 110, 240]
    assert second.scan_now() == _REPORT
    with second.join():
        second.tick()
    assert starts == [0, 110, 240, 360, 360]


def _child(directory: Path, messages: Connection) -> None:
    now = 0.0

    def scan() -> InboxReport:
        messages.send("scanning")
        assert messages.recv() == "complete"
        return _REPORT

    watcher = InboxWatch(directory, scan, monotonic=lambda: now)
    with watcher.join():
        messages.send("joined")
        while (command := messages.recv()) != "leave":
            now = float(command)
            watcher.tick()
            messages.send("settled")
    messages.send("left")


def _receive(connection: Connection) -> object:
    assert connection.poll(5), "watcher did not reply"
    return connection.recv()


@pytest.mark.parametrize("departure", ["parent", "child", "crash"])
def test_processes_handoff_scans_on_departure_and_release_crashed_lock(
    tmp_path: Path,
    departure: str,
) -> None:
    now = 0.0
    starts: list[float] = []

    def scan() -> InboxReport:
        starts.append(now)
        return _REPORT

    parent = InboxWatch(tmp_path, scan, monotonic=lambda: now)
    ctx = get_context("spawn")
    incoming, outgoing = ctx.Pipe()
    child = ctx.Process(target=_child, args=(tmp_path, outgoing))
    child.start()
    try:
        assert _receive(incoming) == "joined"
        with parent.join():
            incoming.send(0)
            assert _receive(incoming) == "scanning"
            parent.tick()
            assert not starts
            if departure == "crash":
                child.terminate()
                child.join(timeout=5)
                assert not child.is_alive()
                parent.tick()
                assert starts == [0]
            else:
                incoming.send("complete")
                assert _receive(incoming) == "settled"
                parent.tick()
                assert not starts
                if departure == "child":
                    incoming.send("leave")
                    assert _receive(incoming) == "left"
                    now = 120
                    parent.tick()
                    assert starts == [120]
        if departure == "parent":
            incoming.send(120)
            assert _receive(incoming) == "scanning"
            incoming.send("complete")
            assert _receive(incoming) == "settled"
            incoming.send("leave")
            assert _receive(incoming) == "left"
        assert parent.cycle() is None
        scans = starts.copy()
        now = 360
        parent.tick()
        assert starts == scans
    finally:
        if child.is_alive():
            child.terminate()
        child.join(timeout=5)
        incoming.close()
        outgoing.close()
        child.close()
