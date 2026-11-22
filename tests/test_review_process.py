"""Verify bounded subprocess output and private scratch cleanup."""

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from loop_reviewer.integrations.review_process import (
    OutputLimitExceeded,
    capture_review_process,
    review_scratch,
)
from loop_reviewer.review.generate import ReviewerError

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="review processes use POSIX pipes"
)


def test_process_drains_stdout_while_sending_input(tmp_path: Path) -> None:
    payload = b"-x\r\n+x\n" * 18000
    result = capture_review_process(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ],
        cwd=tmp_path,
        env=os.environ,
        timeout=5,
        output_limit=len(payload),
        input=payload,
    )
    assert result.returncode == 0
    assert result.stdout == payload


def test_successful_exit_cannot_hide_an_incomplete_input_write(tmp_path: Path) -> None:
    with pytest.raises(BrokenPipeError, match="closed stdin"):
        capture_review_process(
            [sys.executable, "-c", "import os; os.close(0); print('success')"],
            cwd=tmp_path,
            env=os.environ,
            timeout=5,
            output_limit=1024,
            input=b"x" * (128 * 1024),
        )


@pytest.mark.parametrize(
    ("trigger", "error", "timeout"),
    [
        pytest.param(
            "sys.stdout.buffer.write(b'x' * 100000); sys.stdout.flush()",
            OutputLimitExceeded,
            5,
            id="overflow",
        ),
        pytest.param(
            "os.kill(os.getppid(), signal.SIGINT)", KeyboardInterrupt, 5, id="interrupt"
        ),
        pytest.param("pass", subprocess.TimeoutExpired, 2, id="timeout"),
    ],
)
def test_process_reaps_child_on_failure(
    tmp_path: Path, trigger: str, error: type[BaseException], timeout: int
) -> None:
    code = (
        "import os, signal, sys, time\n"
        "from pathlib import Path\n"
        "Path('pid').write_text(str(os.getpid()))\n"
        f"{trigger}\n"
        "time.sleep(8)\n"
    )
    with pytest.raises(error):
        capture_review_process(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            env=os.environ,
            timeout=timeout,
            output_limit=1024,
        )
    pid = int((tmp_path / "pid").read_text())
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


@pytest.mark.parametrize(
    "exit_early", [False, True], ids=["leader-alive", "leader-exited"]
)
def test_timeout_releases_a_lock_inherited_by_descendants(
    tmp_path: Path, exit_early: bool
) -> None:
    import fcntl

    lock = tmp_path / "lock"
    marker = tmp_path / "descendant"
    script = (
        "import os, time\n"
        "from pathlib import Path\n"
        "if os.fork() == 0:\n"
        "    Path('descendant').write_text(str(os.getpid()))\n"
        "    time.sleep(10)\n"
        f"elif not {exit_early!r}:\n"
        "    time.sleep(10)\n"
    )
    with lock.open("w") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        with pytest.raises(subprocess.TimeoutExpired):
            capture_review_process(
                [sys.executable, "-c", script],
                cwd=tmp_path,
                env=os.environ,
                timeout=2,
                output_limit=1024,
                pass_fds=(owner.fileno(),),
            )

    descendant = int(marker.read_text())
    with lock.open("r") as contender:
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    with suppress(ProcessLookupError):
                        os.kill(descendant, signal.SIGKILL)
                    pytest.fail(
                        "A descendant still holds the comparison lock after timeout."
                    )
                time.sleep(0.01)
            else:
                break


def test_scratch_creation_failure_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object, **kwargs: object) -> str:
        raise PermissionError

    monkeypatch.setattr(
        "loop_reviewer.integrations.review_process.tempfile.mkdtemp", denied
    )
    with (
        pytest.raises(ReviewerError, match="Could not create temporary space"),
        review_scratch("git"),
    ):
        pytest.fail("scratch allocation should have failed")


def test_cleanup_failure_warns_without_masking_original_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    def denied(path: Path) -> None:
        raise PermissionError

    def scratch_path(**kwargs: object) -> str:
        return str(tmp_path)

    monkeypatch.setattr(
        "loop_reviewer.integrations.review_process.tempfile.mkdtemp", scratch_path
    )
    monkeypatch.setattr("loop_reviewer.integrations.review_process.rmtree", denied)
    with pytest.raises(ValueError, match="original"), review_scratch("git"):
        raise ValueError("original")
    assert "Could not remove private git scratch" in caplog.text
