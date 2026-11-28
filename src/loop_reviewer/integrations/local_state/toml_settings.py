"""Synchronize editable TOML settings with the verified inbox projection."""

from collections.abc import Generator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast

from tomlkit import array, document, parse, table
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from loop_reviewer.inbox.configuration import (
    InboxInputs,
    LocalConfigurationError,
    LocalSettings,
    normalize_excluded_title_terms,
    saved_inbox_inputs,
)
from loop_reviewer.inbox.discovery import ConfigureInbox
from loop_reviewer.inbox.model import InboxSettings, JiraProjectKey, RepositoryAddress
from loop_reviewer.inbox.store import InboxSettingsChanged, InboxStore, InboxStoreError
from loop_reviewer.integrations.local_state.posix_lock import (
    LocalLockError,
    private_lock,
)


class TomlSettings:
    def __init__(self, path: Path, inbox: ConfigureInbox, store: InboxStore) -> None:
        self._path = path
        self._inbox = inbox
        self._store = store

    def settings(self) -> LocalSettings:
        raw, doc = self._read()
        saved = self._store.settings()
        return self._settings(raw, self._setup_inputs(doc), saved)

    def run(
        self,
        projects: tuple[JiraProjectKey, ...],
        addresses: tuple[RepositoryAddress, ...],
        *,
        excluded_title_terms: tuple[str, ...] | None = None,
        expected_version: int | None = None,
        expected_revision: str | None = None,
    ) -> LocalSettings:
        with self._lock():
            raw, doc = self._read()
            saved = self._store.settings()
            existed = bool(raw) or self._path.exists()
            previous = self._setup_inputs(doc)
            current = self._settings(raw, previous, saved)
            version = current.version or 0
            if (
                expected_version is not None
                and expected_version != version
                or expected_revision is not None
                and expected_revision != current.revision
            ):
                raise InboxSettingsChanged

            terms = normalize_excluded_title_terms(
                current.excluded_title_terms
                if excluded_title_terms is None
                else excluded_title_terms
            )
            repositories = self._inbox.verify_explicit_save(
                (projects, addresses, terms),
                current,
                saved,
                repairing=previous is None and doc.get("inbox") is not None,
            )
            if repositories:
                canonical = tuple(repo.address for repo in repositories)
                inbox = _editable_table(doc, "inbox")
                if (terms or "excluded_title_terms" in inbox) and (
                    previous is None or previous[2] != terms
                ):
                    inbox["excluded_title_terms"] = list(terms)
                if previous is None or previous[0] != projects:
                    inbox["jira_project_key"] = projects[0].value
                if previous is None or previous[1] != canonical:
                    urls = array()
                    for address in canonical:
                        urls.append(address.url)
                    urls.multiline(True)
                    inbox["repositories"] = urls
            updated = doc.as_string().encode()
            if updated != raw:
                self._write(raw, updated)
            else:
                self._check_unchanged(raw)
            if repositories:
                try:
                    saved = self._store.replace_settings(
                        projects,
                        repositories,
                        excluded_title_terms=terms,
                        expected_version=version,
                    )
                except InboxStoreError, InboxSettingsChanged:
                    if updated != raw:
                        self._restore(updated, raw, existed=existed)
                    raise
            return self._settings(updated, self._inputs(doc), saved)

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
            verified = self._inbox.verify_manual_edit(desired, saved)
            if not verified:
                return
            self._check_unchanged(raw)
            self._store.replace_settings(
                desired[0],
                verified,
                excluded_title_terms=desired[2],
                expected_version=saved.version if saved else 0,
            )

    def _settings(
        self, raw: bytes, inputs: InboxInputs | None, saved: InboxSettings | None
    ) -> LocalSettings:
        projects, addresses, terms = inputs or saved_inbox_inputs(saved)
        return LocalSettings(
            projects,
            addresses,
            saved.version if saved else None,
            sha256(raw).hexdigest(),
            terms,
        )

    def _setup_inputs(self, doc: TOMLDocument) -> InboxInputs | None:
        try:
            return self._inputs(doc)
        except LocalConfigurationError:
            return None

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
        terms = fields.get("excluded_title_terms", [])
        if not isinstance(terms, list) or not all(
            isinstance(term, str) for term in terms
        ):
            raise self._error("Set inbox.excluded_title_terms to an array of strings.")
        try:
            return (
                (JiraProjectKey(project),),
                tuple(
                    RepositoryAddress.from_url(url)
                    for url in cast("list[str]", repositories)
                ),
                normalize_excluded_title_terms(tuple(cast("list[str]", terms))),
            )
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

    def _restore(self, updated: bytes, raw: bytes, *, existed: bool) -> None:
        try:
            if existed:
                self._write(updated, raw)
            else:
                path = self._path.resolve()
                self._check_unchanged(updated)
                path.unlink()
        except (InboxSettingsChanged, LocalConfigurationError, OSError) as error:
            raise self._error(
                "Inbox settings were not committed and the configuration could not "
                "be restored. Check the file before retrying."
            ) from error

    def _error(self, message: str) -> LocalConfigurationError:
        return LocalConfigurationError(f"{self._path}: {message}")


def _editable_table(doc: TOMLDocument, name: str) -> Table:
    existing = doc.get(name)
    if isinstance(existing, Table):
        return existing
    new = table()
    doc[name] = new
    return new
