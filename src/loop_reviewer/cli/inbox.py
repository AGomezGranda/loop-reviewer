"""Parse inbox commands and run discovery without a reviewer."""

import sys
from argparse import ArgumentParser, Namespace
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import cast

from loop_reviewer.cli.inbox_output import (
    render_inbox_report,
    render_watch_cycle,
    terminal_text,
)
from loop_reviewer.cli.review import run_review_command
from loop_reviewer.inbox.actions import InboxActions, InboxRowChanged, InboxRowNotFound
from loop_reviewer.inbox.discovery import (
    ConfigureInbox,
    InboxNotConfigured,
    RepositoryDiscoveryError,
    ScanSuperseded,
)
from loop_reviewer.inbox.model import InboxReport, JiraProjectKey, RepositoryAddress
from loop_reviewer.inbox.polling import InboxChanges, ScanCycle
from loop_reviewer.inbox.store import InboxStoreError
from loop_reviewer.review.generate import ReviewOutcome
from loop_reviewer.review.pull_request import PullRequestRef
from loop_reviewer.review.store import ReviewStoreError


def add_inbox_commands(parser: ArgumentParser) -> dict[str, ArgumentParser]:
    commands = parser.add_subparsers(dest="inbox_command", required=True)
    setup = commands.add_parser(
        "setup", help="Verify and replace the repository watch list"
    )
    setup.add_argument(
        "--jira-project",
        action="append",
        required=True,
        metavar="KEY",
        help="Shared Jira project key (specify once)",
    )
    setup.add_argument(
        "--repo",
        action="append",
        required=True,
        metavar="HTTPS_URL",
        help="Repository to watch (repeat for each repository)",
    )
    parsers = {"setup": setup}
    for name, help_text in (
        ("later", "Move a cached inbox row to Later"),
        ("restore", "Move a cached inbox row back to Active"),
        ("open", "Start or resume review from an inbox row"),
    ):
        subcommand = commands.add_parser(name, help=help_text)
        subcommand.add_argument("pr_url", metavar="PR_URL")
        parsers[name] = subcommand
    return parsers


def run_inbox_command(
    command: ArgumentParser,
    args: Namespace,
    *,
    configure: ConfigureInbox,
    scan: Callable[[], InboxReport],
    actions: InboxActions[ReviewOutcome],
    watch: Callable[[], None],
) -> int:
    try:
        if args.command == "inbox" and args.inbox_command == "setup":
            projects = tuple(
                JiraProjectKey(key) for key in cast("list[str]", args.jira_project)
            )
            addresses = tuple(
                RepositoryAddress.from_url(url) for url in cast("list[str]", args.repo)
            )
            settings = configure.run(projects, addresses)
            print(
                f"Configured {len(settings.repositories)} repositories for {settings.prefix_label}. Run loop-reviewer scan."
            )
            return 0
        if args.command == "inbox":
            ref = PullRequestRef.from_url(cast("str", args.pr_url))
            if args.inbox_command == "open":
                return run_review_command(command, lambda: actions.open(ref))
            if args.inbox_command == "later":
                print(f"Moved to Later: {actions.defer(ref).url}")
            else:
                print(f"Restored to active: {actions.restore(ref).url}")
            return 0
        if args.command == "watch":
            watch()
            return 0
        report = scan()
        print(render_inbox_report(report))
        return 0 if report.succeeded else 1
    except InboxRowNotFound as error:
        print(
            f"{command.prog}: error: {error} Run loop-reviewer scan or use loop-reviewer review PR_URL directly.",
            file=sys.stderr,
        )
        return 1
    except InboxRowChanged as error:
        print(
            f"{command.prog}: error: {error} Run loop-reviewer scan and try again.",
            file=sys.stderr,
        )
        return 1
    except (
        ValueError,
        InboxNotConfigured,
        InboxStoreError,
        RepositoryDiscoveryError,
        ReviewStoreError,
        ScanSuperseded,
    ) as error:
        print(f"{command.prog}: error: {terminal_text(str(error))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"{command.prog}: interrupted.", file=sys.stderr)
        return 130


def watch_inbox_command(
    command: ArgumentParser,
    *,
    read: Callable[[], InboxReport],
    join: Callable[[], AbstractContextManager[None]],
    tick: Callable[[], None],
    cycle: Callable[[], ScanCycle | None],
    sleep: Callable[[float], None],
) -> None:
    read()  # Preserve the CLI's immediate missing-setup and local-state errors.
    changes = InboxChanges()
    previous: ScanCycle | None = None
    with join():
        while True:
            tick()
            current = cycle()
            if current is not None and current != previous:
                previous = current
                if current.outcome == "unconfigured":
                    raise InboxNotConfigured
                if current.outcome == "error":
                    raise InboxStoreError("Could not read or scan the local inbox.")
                try:
                    if current.outcome == "superseded":
                        raise ScanSuperseded
                    if current.number:
                        print(render_watch_cycle(changes.observe(read())), flush=True)
                except ScanSuperseded as error:
                    print(
                        f"{command.prog}: {error} Retrying at the next tick.",
                        file=sys.stderr,
                        flush=True,
                    )
            sleep(1)
