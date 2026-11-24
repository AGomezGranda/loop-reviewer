import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

PATH_ENV = {"PATH": os.environ.get("PATH", os.defpath)}
_GIT_ENV = {
    **PATH_ENV,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_ATTR_NOSYSTEM": "1",
}


def git(
    repo: Path,
    *args: str,
    input: bytes | None = None,
    environment: Mapping[str, str] | None = None,
) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**_GIT_ENV, **(environment or {})},
        input=input,
        capture_output=True,
        check=True,
    ).stdout


def init(repo: Path, *, object_format: str = "sha1") -> None:
    git(
        repo,
        "init",
        "-q",
        "--template=",
        "-b",
        "main",
        f"--object-format={object_format}",
    )


def revision(repo: Path, ref: str = "HEAD") -> str:
    return git(repo, "rev-parse", ref).decode("ascii").strip()


def commit(
    repo: Path,
    *,
    stage: bool = True,
    message: str = "fixture",
    environment: Mapping[str, str] | None = None,
) -> str:
    if stage:
        git(repo, "add", "-A")
    git(repo, "commit", "-qm", message, "--allow-empty", environment=environment)
    return revision(repo)
