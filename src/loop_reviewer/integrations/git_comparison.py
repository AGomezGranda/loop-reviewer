"""Fetch and verify pinned pull request objects in a private bare repository."""

import os
import subprocess
import sys
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

from loop_reviewer.integrations.github_credentials import github_environment
from loop_reviewer.integrations.review_process import (
    OutputLimitExceeded,
    capture_review_process,
    with_absolute_path,
)
from loop_reviewer.integrations.review_scratch import review_scratch
from loop_reviewer.review.generate import ReviewerError
from loop_reviewer.review.model import ReviewSelection
from loop_reviewer.review.pull_request import PullRequestRef, ReviewedRevision

_FETCH_TIMEOUT = 90
_VERIFY_TIMEOUT = 60
_MAX_COMMAND_OUTPUT_BYTES = 4096
_MAX_FETCH_FILE_BYTES = 256 * 1024 * 1024
_FETCH_WITH_FILE_LIMIT = """\
import os, resource, signal, sys
limits = (int(sys.argv[1]), *resource.getrlimit(resource.RLIMIT_FSIZE))
size = min(limit for limit in limits if limit != resource.RLIM_INFINITY)
resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
os.execvp(sys.argv[2], sys.argv[2:])
"""
_PREPARATION_FAILED = (
    "Could not prepare the selected comparison from GitHub. Check access and try again."
)


class GitComparison:
    def __init__(
        self, environment: Mapping[str, str], *, inherited_fd: int | None = None
    ) -> None:
        self._environment = with_absolute_path(environment)
        self._pass_fds = () if inherited_fd is None else (inherited_fd,)

    @contextmanager
    def open_verified_repository(self, selection: ReviewSelection) -> Generator[Path]:
        ref = selection.ref
        revision = selection.revision
        with review_scratch("git") as scratch:
            self._run(
                ["git", *_configuration(), "init", "--bare", "--quiet"],
                cwd=scratch,
                timeout=_FETCH_TIMEOUT,
            )
            self._fetch(scratch, ref, revision)
            self._verify(scratch, revision.merge_base, revision.head)
            yield scratch

    def _fetch(
        self, scratch: Path, ref: PullRequestRef, revision: ReviewedRevision
    ) -> None:
        self._run(
            [
                sys.executable,
                "-IS",
                "-c",
                _FETCH_WITH_FILE_LIMIT,
                str(_MAX_FETCH_FILE_BYTES),
                "git",
                *_configuration(),
                # Keep incoming objects in a pack so the file limit covers them together.
                "-c",
                "fetch.unpackLimit=1",
                "fetch",
                "--quiet",
                "--no-tags",
                _remote_url(ref),
                f"+refs/heads/{revision.target_branch}:refs/loop-reviewer/base",
                f"+refs/pull/{ref.number}/head:refs/loop-reviewer/head",
            ],
            cwd=scratch,
            host=ref.host,
            timeout=_FETCH_TIMEOUT,
            failure=(
                "Could not fetch the selected comparison. Check repository access "
                f"and whether its Git pack fits the {_MAX_FETCH_FILE_BYTES / (1024**2):g} MiB fetch limit."
            ),
        )

    def _verify(self, scratch: Path, merge_base: str, head: str) -> None:
        commits = (merge_base, head)
        expressions = [f"{sha}^{{commit}}" for sha in commits]
        objects = self._run(
            ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
            cwd=scratch,
            timeout=_VERIFY_TIMEOUT,
            input=("\n".join(expressions) + "\n").encode("ascii"),
        ).splitlines()
        if len(objects) != len(commits):
            raise ReviewerError(_PREPARATION_FAILED, "failed")
        for sha, expression, result in zip(commits, expressions, objects, strict=True):
            if result == f"{expression} missing".encode("ascii"):
                raise ReviewerError(
                    "A pinned commit was missing from the GitHub fetch.", "failed"
                )
            if result != f"{sha} commit".encode("ascii"):
                raise ReviewerError(_PREPARATION_FAILED, "failed")
        self._run(
            ["git", "merge-base", "--is-ancestor", merge_base, head],
            cwd=scratch,
            timeout=_VERIFY_TIMEOUT,
            unavailable_status=1,
            unavailable=(
                "The recorded merge base is not an ancestor of the selected head. "
                "Select the pull request again with open."
            ),
        )

    def _run(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        timeout: int,
        host: str | None = None,
        unavailable: str | None = None,
        unavailable_status: int | None = None,
        failure: str = _PREPARATION_FAILED,
        input: bytes | None = None,
    ) -> bytes:
        try:
            result = capture_review_process(
                arguments,
                cwd=cwd,
                env=self._environment_for(host),
                timeout=timeout,
                output_limit=_MAX_COMMAND_OUTPUT_BYTES,
                pass_fds=self._pass_fds,
                input=input,
            )
        except FileNotFoundError:
            raise ReviewerError(
                "Install Git and make sure it is on PATH.", "setup"
            ) from None
        except subprocess.TimeoutExpired:
            raise ReviewerError(
                "Preparing the selected comparison timed out; try again.", "failed"
            ) from None
        except OutputLimitExceeded:
            raise ReviewerError(failure, "failed") from None
        except OSError:
            raise ReviewerError(
                "Could not run Git. Check your installation.", "failed"
            ) from None
        if result.returncode != 0:
            if unavailable is not None and result.returncode == unavailable_status:
                raise ReviewerError(unavailable, "input")
            raise ReviewerError(failure, "failed")
        return result.stdout

    def _environment_for(self, host: str | None) -> dict[str, str]:
        child = (
            github_environment(self._environment, host)
            if host is not None
            else {"PATH": self._environment["PATH"]}
        )
        for key in [name for name in child if name.startswith("GIT_")]:
            child.pop(key, None)
        child["GIT_CONFIG_NOSYSTEM"] = "1"
        child["GIT_CONFIG_GLOBAL"] = os.devnull
        child["GIT_TERMINAL_PROMPT"] = "0"
        return child


def _configuration() -> list[str]:
    return [
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "credential.helper=",
        "-c",
        "credential.helper=!gh auth git-credential",
        "-c",
        "http.followRedirects=false",
    ]


def _remote_url(ref: PullRequestRef) -> str:
    return f"https://{ref.host}/{ref.owner}/{ref.repo}.git"
