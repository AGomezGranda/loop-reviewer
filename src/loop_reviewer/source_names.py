"""Validate Git and GitHub names shared by hosted selection and report contracts."""

import re
from unicodedata import category

_DNS_LABEL = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
_HOST = re.compile(rf"{_DNS_LABEL}(?:\.{_DNS_LABEL})*")
_OWNER = r"[a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)*"
_REPOSITORY = re.compile(rf"({_OWNER})/([a-zA-Z0-9_.-]{{1,100}})")


def validate_repository(host: str, owner: str, repo: str) -> None:
    if len(host) > 253 or not _HOST.fullmatch(host):
        raise ValueError("Expected a valid GitHub hostname")
    if not _REPOSITORY.fullmatch(f"{owner}/{repo}") or repo in {".", ".."}:
        raise ValueError("Expected a GitHub repository in OWNER/REPO form")


def validate_target_branch(branch: str) -> None:
    if (
        any(char.isspace() or category(char) in {"Cc", "Cs"} for char in branch)
        or any(char in branch for char in "~^:?*[\\")
        or ".." in branch
        or "@{" in branch
        or branch.endswith(".")
        or any(
            not part or part.startswith(".") or part.endswith(".lock")
            for part in branch.split("/")
        )
    ):
        raise ValueError("Expected a valid Git target branch")
