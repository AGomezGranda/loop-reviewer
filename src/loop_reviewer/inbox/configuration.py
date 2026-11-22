"""Expose editable local settings to the CLI and browser workflows."""

from dataclasses import dataclass
from typing import Protocol

from loop_reviewer.inbox.model import JiraProjectKey, RepositoryAddress
from loop_reviewer.inbox.store import InboxStoreError


class LocalConfigurationError(InboxStoreError):
    pass


@dataclass(frozen=True)
class LocalSettings:
    projects: tuple[JiraProjectKey, ...]
    addresses: tuple[RepositoryAddress, ...]
    version: int | None
    revision: str
    model: str | None


class ConfigureLocal(Protocol):
    def settings(self) -> LocalSettings: ...

    def run(
        self,
        projects: tuple[JiraProjectKey, ...],
        addresses: tuple[RepositoryAddress, ...],
        *,
        expected_version: int | None = None,
        expected_revision: str | None = None,
        model: str | None = None,
    ) -> LocalSettings: ...
