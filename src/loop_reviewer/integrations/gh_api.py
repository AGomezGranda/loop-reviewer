"""Invoke gh and separate HTTP metadata from the response body."""

import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from loop_reviewer.integrations.github_credentials import github_environment
from loop_reviewer.integrations.review_process import (
    OutputLimitExceeded,
    capture_review_process,
)

_HTTP_STATUS = re.compile(r"HTTP/[0-9]+(?:\.[0-9]+)? ([1-5][0-9]{2})(?: .*)?")


@dataclass(frozen=True)
class GhResponse:
    status: int | None
    headers: Mapping[str, str]
    body: str
    returncode: int


class GhInvocationError(Exception):
    def __init__(
        self, reason: Literal["missing", "timeout", "execution", "encoding", "output"]
    ) -> None:
        super().__init__(reason)
        self.reason = reason


class GhApi:
    def __init__(
        self, environment: Mapping[str, str], *, private_cwd: Path | None = None
    ) -> None:
        self._environment = dict(environment)
        self._private_cwd = private_cwd

    def get(self, host: str, endpoint: str) -> GhResponse:
        if self._private_cwd is not None:
            return self._bounded(
                host, gh_arguments(host, "GET", endpoint), output_limit=8 * 1024**2
            )
        try:
            result = subprocess.run(
                gh_arguments(host, "GET", endpoint),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
                check=False,
                timeout=15,
                env=github_environment(self._environment, host),
            )
        except FileNotFoundError:
            raise GhInvocationError("missing") from None
        except subprocess.TimeoutExpired:
            raise GhInvocationError("timeout") from None
        except OSError:
            raise GhInvocationError("execution") from None
        except UnicodeError:
            raise GhInvocationError("encoding") from None
        return http_response(result.stdout, result.returncode)

    def create(
        self, host: str, endpoint: str, payload: bytes, *, inherited_fd: int
    ) -> GhResponse:
        return self._bounded(
            host,
            gh_arguments(
                host,
                "POST",
                endpoint,
                "--header",
                "Content-Type: application/json",
                "--input",
                "-",
            ),
            output_limit=1024**2,
            input=payload,
            pass_fds=(inherited_fd,),
        )

    def _bounded(
        self,
        host: str,
        arguments: list[str],
        *,
        output_limit: int,
        input: bytes | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> GhResponse:
        assert self._private_cwd is not None
        try:
            result = capture_review_process(
                arguments,
                cwd=self._private_cwd,
                env=github_environment(self._environment, host),
                timeout=15,
                output_limit=output_limit,
                input=input,
                pass_fds=pass_fds,
            )
            return http_response(result.stdout.decode("utf-8"), result.returncode)
        except FileNotFoundError:
            raise GhInvocationError("missing") from None
        except subprocess.TimeoutExpired:
            raise GhInvocationError("timeout") from None
        except OSError:
            raise GhInvocationError("execution") from None
        except UnicodeError:
            raise GhInvocationError("encoding") from None
        except OutputLimitExceeded:
            raise GhInvocationError("output") from None


def gh_arguments(host: str, method: str, endpoint: str, *options: str) -> list[str]:
    return [
        "gh",
        "api",
        "--hostname",
        host,
        "--method",
        method,
        "--include",
        *options,
        endpoint,
    ]


def http_response(stdout: str, returncode: int) -> GhResponse:
    head, *rest = re.split(r"\r?\n\r?\n", stdout, maxsplit=1)
    status_line, *lines = head.splitlines() or [""]
    status = _HTTP_STATUS.fullmatch(status_line) if rest else None
    headers: dict[str, str] = {}
    for line in lines:
        name, colon, value = line.partition(":")
        if colon:
            headers[name.lower()] = value.strip()
    return GhResponse(
        int(status[1]) if status else None, headers, rest[0] if rest else "", returncode
    )
