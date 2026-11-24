"""Scope GitHub credentials to the target host and strip them from reviewer processes."""

import os
import re
import subprocess
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from loop_reviewer.integrations.review_process import (
    OutputLimitExceeded,
    capture_review_process,
    with_absolute_path,
)
from loop_reviewer.review.selection import GitHubReadError

_PUBLIC_TOKENS = ("GH_TOKEN", "GITHUB_TOKEN")
_ENTERPRISE_TOKENS = ("GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
_TOKEN_LIMIT = 16_384


def github_environment(environment: Mapping[str, str], host: str) -> dict[str, str]:
    if host == "github.com":
        removed_tokens = _ENTERPRISE_TOKENS
    elif host == environment.get("GH_HOST", "").lower():
        removed_tokens = ()
    else:
        removed_tokens = _PUBLIC_TOKENS + _ENTERPRISE_TOKENS
    return _without(environment, removed_tokens)


def capture_gh_environment(
    environment: Mapping[str, str], host: str, *, cwd: Path | None = None
) -> dict[str, str]:
    environment = with_absolute_path(environment)
    scoped = github_environment(environment, host)
    _disable_debug(scoped)
    guidance = f"Could not capture the GitHub credential. Sign in with gh auth login --hostname {host}."
    arguments = ["gh", "auth", "token", "--hostname", host]
    try:
        if os.name == "posix":
            result = capture_review_process(
                arguments,
                cwd=Path.cwd() if cwd is None else cwd,
                env=scoped,
                timeout=15,
                output_limit=_TOKEN_LIMIT,
            )
        else:
            result = subprocess.run(
                arguments,
                cwd=cwd,
                env=scoped,
                timeout=15,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        if len(result.stdout) > _TOKEN_LIMIT:
            raise OutputLimitExceeded
        token = re.fullmatch(rb"([!-~]+)(?:\r?\n)?", result.stdout)
        if result.returncode or token is None:
            raise GitHubReadError(guidance)
    except FileNotFoundError:
        raise GitHubReadError(
            "Install the GitHub CLI (gh) and make sure it is on PATH."
        ) from None
    except subprocess.TimeoutExpired:
        raise GitHubReadError(
            "Reading the GitHub credential timed out; try again."
        ) from None
    except OSError:
        raise GitHubReadError(
            "Could not run the GitHub CLI (gh). Check your installation."
        ) from None
    except OutputLimitExceeded:
        raise GitHubReadError(
            "The GitHub CLI returned an oversized credential response."
        ) from None
    pinned = without_github_credentials(environment)
    _disable_debug(pinned)
    pinned["GH_HOST"] = host
    pinned[
        "GH_TOKEN"
        if host == "github.com" or host.endswith(".ghe.com")
        else "GH_ENTERPRISE_TOKEN"
    ] = token[1].decode("ascii")
    return pinned


@contextmanager
def open_gh_environment(
    environment: Mapping[str, str],
    host: str,
    *,
    prefix: str,
    private_capture_cwd: bool = False,
) -> Generator[tuple[dict[str, str], Path]]:
    pinned = None if private_capture_cwd else capture_gh_environment(environment, host)
    try:
        config = TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True)
    except OSError:
        raise GitHubReadError("Could not prepare private GitHub CLI state.") from None
    with config as directory:
        path = Path(directory)
        if pinned is None:
            pinned = capture_gh_environment(environment, host, cwd=path)
        # The empty configuration prevents later commands from loading a different account.
        pinned["GH_CONFIG_DIR"] = directory
        yield pinned, path


def _disable_debug(environment: dict[str, str]) -> None:
    for name in ("GH_DEBUG", "DEBUG", "CLICOLOR_FORCE"):
        environment.pop(name, None)
    environment["NO_COLOR"] = "1"
    environment["GH_TELEMETRY"] = "disabled"


def without_github_credentials(environment: Mapping[str, str]) -> dict[str, str]:
    return _without(environment, _PUBLIC_TOKENS + _ENTERPRISE_TOKENS)


def _without(environment: Mapping[str, str], tokens: tuple[str, ...]) -> dict[str, str]:
    child = dict(environment)
    for token in tokens:
        child.pop(token, None)
    child.pop("GH_FORCE_TTY", None)
    child["GH_PROMPT_DISABLED"] = "1"
    return child
