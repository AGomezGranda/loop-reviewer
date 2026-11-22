"""Synchronize editable TOML settings with the verified inbox projection."""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast

from tomlkit import array, document, parse, table
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from loop_reviewer.inbox.configuration import LocalConfigurationError, LocalSettings
from loop_reviewer.inbox.discovery import ConfigureInbox, RepositoryDiscoveryError
from loop_reviewer.inbox.model import InboxSettings, JiraProjectKey, RepositoryAddress
from loop_reviewer.inbox.store import InboxSettingsChanged, InboxStore
from loop_reviewer.integrations.local_state.posix_lock import (
    LocalLockError,
    private_lock,
)

type InboxInputs = tuple[tuple[JiraProjectKey, ...], tuple[RepositoryAddress, ...]]


class TomlSettings:
    def __init__(self, path: Path, inbox: ConfigureInbox, store: InboxStore) -> None:
        self._path = path
        self._inbox = inbox
        self._store = store

    def settings(self) -> LocalSettings:
        raw, doc = self._read()
        saved = self._store.settings()
        return self._settings(raw, doc, saved)

    def model(self) -> str | None:
        return self._model(self._read()[1])

    def run(
        self,
        projects: tuple[JiraProjectKey, ...],
        addresses: tuple[RepositoryAddress, ...],
        *,
        expected_version: int | None = None,
        expected_revision: str | None = None,
        model: str | None = None,
    ) -> LocalSettings:
        with self._lock():
            raw, doc = self._read()
            saved = self._store.settings()
            current = self._settings(raw, doc, saved)
            version = current.version or 0
            if (
                expected_version is not None
                and expected_version != version
                or expected_revision is not None
                and expected_revision != current.revision
            ):
                raise InboxSettingsChanged

            if model:
                model = _validate_model(model)
            # An unchanged inbox can be left alone when saving only the model.
            changed = (projects, addresses) != (current.projects, current.addresses)
            model_changed = model is not None and (model or None) != current.model
            verify_inbox = bool(projects or addresses) and (
                changed or not model_changed
            )
            if not (projects or addresses) and current.projects:
                raise ValueError(
                    "Configure a Jira project key and at least one repository."
                )
            repositories = (
                self._inbox.verify(projects, addresses) if verify_inbox else ()
            )
            if repositories:
                canonical = tuple(repo.address for repo in repositories)
                previous = self._inputs(doc)
                inbox = _editable_table(doc, "inbox")
                if previous is None or previous[0] != projects:
                    inbox["jira_project_key"] = projects[0].value
                if previous is None or previous[1] != canonical:
                    urls = array()
                    for address in canonical:
                        urls.append(address.url)
                    urls.multiline(True)
                    inbox["repositories"] = urls
            if model_changed:
                reviewer = _editable_table(doc, "reviewer")
                reviewer["engine"] = "pi"
                if model:
                    reviewer["model"] = model
                else:
                    del reviewer["model"]
            updated = doc.as_string().encode()
            if updated != raw:
                self._write(raw, updated)
            else:
                self._check_unchanged(raw)
            if repositories:
                saved = self._store.replace_settings(
                    projects, repositories, expected_version=version
                )
            return self._settings(updated, doc, saved)

    def sync_inbox(self) -> None:
        # Read-only operations on a legacy inbox do not create a configuration directory.
        raw = self._bytes()
        if not raw:
            return
        with self._lock():
            raw, doc = self._read()
            desired = self._inputs(doc)
            if desired is None:
                return
            saved = self._store.settings()
            if desired == _saved_inputs(saved):
                return
            projects, addresses = desired
            verified = self._inbox.verify(projects, addresses)
            if saved is not None:
                accounts = {
                    repo.address.host: repo.account for repo in saved.repositories
                }
                identities = {
                    (repo.address.host, repo.address.slug.casefold()): repo.key
                    for repo in saved.repositories
                }
                for address, repo in zip(addresses, verified, strict=True):
                    if (
                        accounts.get(address.host, repo.account) != repo.account
                        or identities.get(
                            (address.host, address.slug.casefold()), repo.key
                        )
                        != repo.key
                    ):
                        raise RepositoryDiscoveryError(
                            "The GitHub account or repository identity changed. Verify the new watch list explicitly in Setup."
                        )
            self._check_unchanged(raw)
            self._store.replace_settings(
                projects,
                tuple(
                    replace(repo, address=address)
                    for address, repo in zip(addresses, verified, strict=True)
                ),
                expected_version=saved.version if saved else 0,
            )

    def _settings(
        self, raw: bytes, doc: TOMLDocument, saved: InboxSettings | None
    ) -> LocalSettings:
        projects, addresses = self._inputs(doc) or _saved_inputs(saved)
        return LocalSettings(
            projects,
            addresses,
            saved.version if saved else None,
            sha256(raw).hexdigest(),
            self._model(doc),
        )

    def _inputs(self, doc: TOMLDocument) -> InboxInputs | None:
        inbox = doc.get("inbox")
        if inbox is None:
            return None
        fields = inbox.unwrap() if isinstance(inbox, Table) else {}
        project, repositories = (
            fields.get("jira_project_key"),
            fields.get("repositories"),
        )
        if (
            not isinstance(project, str)
            or not isinstance(repositories, list)
            or not repositories
            or not all(isinstance(url, str) for url in repositories)
        ):
            raise self._error(
                "Set inbox.jira_project_key and a nonempty inbox.repositories array."
            )
        try:
            return (JiraProjectKey(project),), tuple(
                RepositoryAddress.from_url(url)
                for url in cast("list[str]", repositories)
            )
        except ValueError as error:
            raise self._error(str(error)) from None

    def _model(self, doc: TOMLDocument) -> str | None:
        reviewer = doc.get("reviewer")
        if reviewer is None:
            return None
        if not isinstance(reviewer, Table) or reviewer.get("engine", "pi") != "pi":
            raise self._error('Set reviewer.engine to "pi".')
        model = reviewer.get("model")
        if model is None:
            return None
        try:
            return _validate_model(model)
        except ValueError as error:
            raise self._error(str(error)) from None

    def _bytes(self) -> bytes:
        try:
            return self._path.read_bytes()
        except FileNotFoundError:
            return b""
        except OSError:
            raise self._error("Could not read the file.") from None

    def _read(self) -> tuple[bytes, TOMLDocument]:
        raw = self._bytes()
        try:
            return raw, parse(raw.decode()) if raw else document()
        except TOMLKitError, UnicodeError, RecursionError:
            raise self._error("Expected a valid UTF-8 TOML file.") from None

    def _check_unchanged(self, raw: bytes) -> None:
        if self._bytes() != raw:
            raise InboxSettingsChanged

    @contextmanager
    def _lock(self) -> Generator[None]:
        try:
            with private_lock(
                self._path.parent / ".locks", "config.lock", blocking=True
            ):
                yield
        except LocalLockError:
            raise self._error("Could not lock the file for updating.") from None

    def _write(self, previous: bytes, data: bytes) -> None:
        try:
            path = self._path.resolve()
            with NamedTemporaryFile(dir=path.parent, delete_on_close=False) as stream:
                stream.write(data)
                stream.flush()
                self._check_unchanged(previous)
                Path(stream.name).replace(path)
        except OSError:
            raise self._error(
                "Could not save the file. Check directory permissions."
            ) from None

    def _error(self, message: str) -> LocalConfigurationError:
        return LocalConfigurationError(f"{self._path}: {message}")


def _editable_table(doc: TOMLDocument, name: str) -> Table:
    existing = doc.get(name)
    if isinstance(existing, Table):
        return existing
    new = table()
    doc[name] = new
    return new


def _saved_inputs(saved: InboxSettings | None) -> InboxInputs:
    if saved is None:
        return (), ()
    return saved.projects, tuple(repo.address for repo in saved.repositories)


def _validate_model(model: object) -> str:
    if (
        isinstance(model, str)
        and model.isprintable()
        and not any(char.isspace() for char in model)
    ):
        provider, _, name = model.partition("/")
        if provider and name:
            return model
    raise ValueError("Set the Pi model in PROVIDER/MODEL format.")
