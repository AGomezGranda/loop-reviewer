"""Expose editable local settings to the CLI and browser workflows."""

from dataclasses import dataclass
from typing import Protocol

from loop_reviewer.inbox.model import InboxSettings, JiraProjectKey, RepositoryAddress
from loop_reviewer.inbox.store import InboxStoreError


class LocalConfigurationError(InboxStoreError):
    pass


@dataclass(frozen=True)
class LocalSettings:
    projects: tuple[JiraProjectKey, ...]
    addresses: tuple[RepositoryAddress, ...]
    version: int | None
    revision: str
    excluded_title_terms: tuple[str, ...] = ()


type InboxInputs = tuple[
    tuple[JiraProjectKey, ...], tuple[RepositoryAddress, ...], tuple[str, ...]
]


def normalize_excluded_title_terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    unique: dict[str, str] = {}
    for term in terms:
        if "\n" in term or "\r" in term:
            raise ValueError("Enter each excluded title term on a single line.")
        if stripped := term.strip():
            unique.setdefault(stripped.casefold(), stripped)
    return tuple(unique.values())


def saved_inbox_inputs(saved: InboxSettings | None) -> InboxInputs:
    if saved is None:
        return (), (), ()
    return (
        saved.projects,
        tuple(repo.address for repo in saved.repositories),
        saved.excluded_title_terms,
    )


class ConfigureLocal(Protocol):
    def settings(self) -> LocalSettings: ...

    def run(
        self,
        projects: tuple[JiraProjectKey, ...],
        addresses: tuple[RepositoryAddress, ...],
        *,
        excluded_title_terms: tuple[str, ...] | None = None,
        expected_version: int | None = None,
        expected_revision: str | None = None,
    ) -> LocalSettings: ...
