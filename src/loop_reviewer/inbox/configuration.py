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


type InboxInputs = tuple[tuple[JiraProjectKey, ...], tuple[RepositoryAddress, ...]]


def saved_inbox_inputs(saved: InboxSettings | None) -> InboxInputs:
    if saved is None:
        return (), ()
    return saved.projects, tuple(repo.address for repo in saved.repositories)


def verify_inbox_on_save(
    requested: InboxInputs,
    current: LocalSettings,
) -> bool:
    if not any(requested):
        if current.projects:
            raise ValueError(
                "Configure a Jira project key and at least one repository."
            )
        return False
    return True


class ConfigureLocal(Protocol):
    def settings(self) -> LocalSettings: ...

    def run(
        self,
        projects: tuple[JiraProjectKey, ...],
        addresses: tuple[RepositoryAddress, ...],
        *,
        expected_version: int | None = None,
        expected_revision: str | None = None,
    ) -> LocalSettings: ...
