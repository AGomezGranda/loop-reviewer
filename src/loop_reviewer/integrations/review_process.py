"""Run review subprocesses with bounded output and private scratch directories."""

import os
import selectors
import signal
import subprocess
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from logging import getLogger
from pathlib import Path
from shutil import rmtree
from time import monotonic

from loop_reviewer.review.generate import ReviewerError


class OutputLimitExceeded(Exception):
    pass


def with_absolute_path(environment: Mapping[str, str]) -> dict[str, str]:
    child = dict(environment)
    if "PATH" in child:
        child["PATH"] = os.pathsep.join(
            os.path.abspath(entry) for entry in child["PATH"].split(os.pathsep)
        )
    return child


@contextmanager
def review_scratch(purpose: str) -> Generator[Path]:
    try:
        path = Path(tempfile.mkdtemp(prefix=f"loop-reviewer-{purpose}-"))
    except OSError as error:
        raise ReviewerError(
            f"Could not create temporary space for {purpose}. Check permissions and free space.",
            "failed",
        ) from error
    try:
        yield path
    finally:
        try:
            rmtree(path)
        except FileNotFoundError:
            pass
        except OSError:
            getLogger(__name__).warning(
                "Could not remove private %s scratch data.", purpose
            )


def capture_review_process(
    arguments: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
    output_limit: int,
    input: bytes | None = None,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    deadline = monotonic() + timeout
    with subprocess.Popen(
        arguments,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        pass_fds=pass_fds,
        start_new_session=True,
    ) as child:
        try:
            assert child.stdout is not None
            output = bytearray()
            pending = memoryview(input or b"")
            incomplete_input = False
            with selectors.DefaultSelector() as selector:
                stdout = child.stdout.fileno()
                os.set_blocking(stdout, False)
                selector.register(stdout, selectors.EVENT_READ)
                if child.stdin is not None:
                    stdin = child.stdin.fileno()
                    os.set_blocking(stdin, False)
                    selector.register(stdin, selectors.EVENT_WRITE)
                while selector.get_map():
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(arguments, timeout)
                    for key, event in selector.select(remaining):
                        if event == selectors.EVENT_READ:
                            try:
                                data = os.read(
                                    key.fd, min(65536, output_limit - len(output) + 1)
                                )
                            except BlockingIOError:
                                continue
                            if not data:
                                selector.unregister(key.fd)
                                continue
                            output.extend(data)
                            if len(output) > output_limit:
                                raise OutputLimitExceeded
                        else:
                            try:
                                pending = pending[os.write(key.fd, pending) :]
                            except BlockingIOError:
                                continue
                            except BrokenPipeError:
                                incomplete_input = True
                            if incomplete_input or not pending:
                                selector.unregister(key.fd)
                                assert child.stdin is not None
                                child.stdin.close()
            status = child.wait(timeout=max(0, deadline - monotonic()))
        except BaseException:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                child.wait()
            raise
    if incomplete_input:
        raise BrokenPipeError("The process closed stdin before receiving all input.")
    return subprocess.CompletedProcess(arguments, status, bytes(output))
