"""Parse CLI commands, wire dependencies, and dispatch review operations."""

import os
import sys
from argparse import ArgumentParser, ArgumentTypeError, Namespace
from io import TextIOWrapper
from pathlib import Path
from stat import S_ISREG
from tempfile import NamedTemporaryFile
from time import monotonic, sleep

from fastapi import FastAPI

from loop_reviewer.cli.inbox import (
    add_inbox_commands,
    run_inbox_command,
    watch_inbox_command,
)
from loop_reviewer.cli.output import (
    render_completed_review,
    render_draft,
    selection_lines,
)
from loop_reviewer.cli.publication import (
    add_publication_commands,
    run_publication,
    unconfirmed_publication_guidance,
)
from loop_reviewer.cli.review import run_review_command
from loop_reviewer.composition import Workflows
from loop_reviewer.local_http.app import create_app
from loop_reviewer.local_http.serve import serve
from loop_reviewer.review.drafts import DraftNotFound
from loop_reviewer.review.feedback import feedback_markdown
from loop_reviewer.review.model import (
    MAX_EDIT_BYTES,
    MAX_STORED_INTEGER,
    DraftTarget,
    DraftValidationError,
    InvalidRunId,
    ReviewAlreadyCompleted,
)
from loop_reviewer.review.pull_request import (
    PullRequestNotReviewable,
    PullRequestRef,
)
from loop_reviewer.review.selection import GitHubReadError
from loop_reviewer.review.store import (
    DraftConflict,
    ReviewStoreError,
    UnconfirmedPublication,
)


def main() -> int:
    parser = ArgumentParser(prog="loop-reviewer")
    commands = parser.add_subparsers(dest="command")
    open_command = commands.add_parser(
        "open", help="Select a pull request revision for review"
    )
    open_command.add_argument(
        "pr_url", metavar="PR_URL", help="URL of an open, ready pull request"
    )
    open_command.add_argument(
        "--discussion-snapshot",
        type=Path,
        metavar="PATH",
        help="Save a private discussion snapshot for the selected revision",
    )
    review_command = commands.add_parser(
        "review", help="Generate or resume a private draft, or view completed history"
    )
    review_command.add_argument(
        "--retry",
        action="store_true",
        help="Replace a failed or abandoned attempt for the same selection",
    )
    review_command.add_argument(
        "pr_url", metavar="PR_URL", help="URL of a readable pull request"
    )
    draft_commands = _add_draft_commands(
        commands.add_parser("draft", help="Work with a saved draft by local run ID")
    )
    publication_commands = add_publication_commands(
        commands.add_parser(
            "publish", help="Preview, publish or recover a GitHub review"
        ),
        _stored_integer,
    )
    inbox_commands = add_inbox_commands(
        commands.add_parser("inbox", help="Configure the review inbox")
    )
    scan_command = commands.add_parser("scan", help="Refresh and show the review inbox")
    watch_command = commands.add_parser(
        "watch", help="Refresh the inbox every two minutes"
    )
    commands.add_parser("ui", help="Open the private review workspace in a browser")
    args = parser.parse_args()
    if isinstance(sys.stdout, TextIOWrapper) and sys.stdout.encoding != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    if args.command is None:
        return 0
    if args.command == "ui":
        return _run_ui()
    if args.command == "draft":
        return _run_draft(draft_commands[args.draft_command], args)
    if args.command == "publish":
        return _run_publication(publication_commands[args.publish_command], args)
    if args.command == "inbox":
        return _run_inbox(inbox_commands[args.inbox_command], args)
    if args.command in {"scan", "watch"}:
        return _run_inbox(
            scan_command if args.command == "scan" else watch_command, args
        )
    command = open_command if args.command == "open" else review_command

    try:
        ref = PullRequestRef.from_url(args.pr_url)
    except ValueError as error:
        command.error(str(error))

    if args.command == "review":
        return run_review_command(
            command, lambda: (ref, _workflows().review(ref, retry=args.retry))
        )
    workflows = _workflows()
    try:
        selection = workflows.select(ref)
    except (GitHubReadError, PullRequestNotReviewable) as error:
        command.exit(1, f"{command.prog}: error: {error}\n")
    print("\n".join(selection_lines(selection)))
    destination = args.discussion_snapshot
    if destination is None:
        return 0
    try:
        snapshot = workflows.capture_discussion(selection)
        if snapshot.status == "unavailable":
            reason = snapshot.omissions[0].code
            command.exit(1, f"{command.prog}: discussion unavailable ({reason}).\n")
        with NamedTemporaryFile(
            dir=destination.parent, prefix=".discussion-"
        ) as stream:
            stream.write(snapshot.model_dump_json().encode("utf-8"))
            stream.flush()
            os.link(stream.name, destination)
    except ValueError:
        command.exit(1, f"{command.prog}: could not capture the selected discussion.\n")
    except FileExistsError:
        command.exit(1, f"{command.prog}: the snapshot destination already exists.\n")
    except OSError:
        command.exit(1, f"{command.prog}: could not save the private snapshot.\n")
    print(f"Discussion snapshot saved ({snapshot.status}).")
    return 0 if snapshot.status == "complete" else 3


def _run_ui() -> int:
    workflows = _workflows()
    watcher = workflows.inbox_watch(monotonic=monotonic)

    def create(origin: str, capability: str, assets: Path) -> FastAPI:
        return create_app(
            origin=origin,
            capability=capability,
            assets=assets,
            read_inbox=workflows.read_inbox,
            read_cached_inbox=workflows.read_cached_inbox,
            scan_inbox=watcher.scan_now,
            configure_inbox=workflows.configure_inbox(),
            inbox_actions=workflows.inbox_actions(),
            review_pull_request=lambda ref, retry: workflows.review(ref, retry=retry),
            drafts=workflows.edit_review_draft(),
            review_history=workflows.review_history(),
            prepare=workflows.prepare_publication(),
            publish=workflows.publish_review(),
            join_watch=watcher.join,
            watch_tick=watcher.tick,
            watch_cycle=watcher.cycle,
        )

    return serve(create)


def _run_inbox(command: ArgumentParser, args: Namespace) -> int:
    workflows = _workflows()
    watcher = workflows.inbox_watch(monotonic=monotonic)
    return run_inbox_command(
        command,
        args,
        configure=workflows.configure_inbox(),
        scan=watcher.scan_now,
        actions=workflows.inbox_actions(),
        watch=lambda: watch_inbox_command(
            command,
            read=workflows.read_inbox,
            join=watcher.join,
            tick=watcher.tick,
            cycle=watcher.cycle,
            sleep=sleep,
        ),
    )


def _run_publication(command: ArgumentParser, args: Namespace) -> int:
    workflows = _workflows()
    return run_publication(
        command,
        args,
        workflows.prepare_publication(),
        workflows.publish_review(),
        _discard_failed_stdout,
    )


def _discard_failed_stdout() -> None:
    # A failed buffered flush must not replace the command's exit status at shutdown.
    if isinstance(sys.stdout, TextIOWrapper):
        try:
            with open(os.devnull, "wb") as sink:
                os.dup2(sink.fileno(), sys.stdout.fileno())
        except OSError, ValueError:
            pass


def _add_draft_commands(parser: ArgumentParser) -> dict[str, ArgumentParser]:
    commands = parser.add_subparsers(dest="draft_command", required=True)
    parsers: dict[str, ArgumentParser] = {}
    for name, help_text in (
        ("show", "Show the saved draft and its version"),
        ("edit-summary", "Replace the draft summary from a file"),
        ("edit-finding", "Replace finding feedback from a file"),
        ("drop-finding", "Remove a finding from the draft"),
        ("copy", "Print feedback as Markdown to stdout"),
        (
            "finish",
            "Close the initial review for the whole PR locally; never post to GitHub",
        ),
    ):
        command = commands.add_parser(name, help=help_text, description=help_text)
        command.add_argument("run_id", metavar="RUN_ID", type=_stored_integer)
        if name not in {"show", "copy"}:
            command.add_argument(
                "--version", metavar="N", type=_stored_integer, required=True
            )
        if name in {"edit-finding", "drop-finding"}:
            command.add_argument("number", metavar="NUMBER", type=_positive_integer)
        if name == "edit-summary":
            command.add_argument("--file", metavar="PATH", type=Path, required=True)
        if name == "edit-finding":
            command.add_argument("--condition-file", metavar="PATH", type=Path)
            command.add_argument("--consequence-file", metavar="PATH", type=Path)
        parsers[name] = command
    return parsers


def _positive_integer(text: str) -> int:
    try:
        value = int(text)
        if value >= 1:
            return value
    except ValueError:
        pass
    raise ArgumentTypeError("expected a positive integer")


def _stored_integer(text: str) -> int:
    value = _positive_integer(text)
    if value > MAX_STORED_INTEGER:
        raise ArgumentTypeError("value is outside the supported integer range")
    return value


def _read_edit_file(path: Path) -> str:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            if not S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise DraftValidationError("Edit input must be a regular file.")
            data = stream.read(MAX_EDIT_BYTES + 1)
    except OSError as error:
        raise DraftValidationError(
            f"Could not read edit file {str(path)!r}."
        ) from error
    if len(data) > MAX_EDIT_BYTES:
        raise DraftValidationError("Edit files must be at most 64 KiB per field.")
    try:
        return data.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError as error:
        raise DraftValidationError("Edit files must contain valid UTF-8.") from error


def _run_draft(command: ArgumentParser, args: Namespace) -> int:
    action = args.draft_command
    if (
        action == "edit-finding"
        and args.condition_file is None
        and args.consequence_file is None
    ):
        command.error("provide at least one of --condition-file or --consequence-file")
    application = _workflows().edit_review_draft()
    try:
        if action in {"show", "copy"}:
            run = application.show(args.run_id)
        else:
            target = DraftTarget(args.run_id, args.version)
            if action == "finish":
                completed = application.finish_locally(target)
                return _print_saved(
                    command,
                    render_completed_review(completed),
                    "Review finished locally"
                    if completed.receipt is None
                    else "Review already published",
                )
            if action == "edit-summary":
                run = application.edit_summary(target, _read_edit_file(args.file))
            elif action == "edit-finding":
                run = application.edit_finding(
                    target,
                    args.number,
                    condition=None
                    if args.condition_file is None
                    else _read_edit_file(args.condition_file),
                    consequence=None
                    if args.consequence_file is None
                    else _read_edit_file(args.consequence_file),
                )
            else:
                run = application.drop_finding(target, args.number)
    except InvalidRunId as error:
        command.error(str(error))
    except (
        DraftNotFound,
        DraftValidationError,
        ReviewAlreadyCompleted,
        ReviewStoreError,
    ) as error:
        print(f"{command.prog}: error: {_draft_error(error)}", file=sys.stderr)
        return 1
    saved = run.saved_draft()
    if action not in {"show", "copy"}:
        return _print_saved(
            command,
            render_draft(saved),
            f"Draft {saved.id} saved at version {saved.draft_version}",
        )
    print(feedback_markdown(saved.draft) if action == "copy" else render_draft(saved))
    return 0


def _draft_error(error: Exception) -> str:
    if isinstance(error, UnconfirmedPublication):
        return unconfirmed_publication_guidance(error)
    if isinstance(error, DraftConflict):
        return f"{error} Run loop-reviewer draft show again before retrying."
    if isinstance(error, ReviewAlreadyCompleted):
        ref = error.completion.selection.ref
        url = f"https://{ref.host}/{ref.owner}/{ref.repo}/pull/{ref.number}"
        return f"{error} Run loop-reviewer review {url} to view history."
    return str(error)


def _print_saved(command: ArgumentParser, text: str, saved: str) -> int:
    try:
        print(text, flush=True)
    except OSError, UnicodeError:
        _discard_failed_stdout()
        print(
            f"{command.prog}: {saved}, but could not write the receipt to stdout.",
            file=sys.stderr,
        )
        return 1
    return 0


def _workflows() -> Workflows:
    environment = os.environ.copy()
    xdg = environment.get("XDG_CONFIG_HOME", "")
    config = Path(xdg) if os.path.isabs(xdg) else Path.home() / ".config"
    return Workflows(
        _state_directory(),
        environment,
        config_path=config / "loop-reviewer" / "config.toml",
    )


def _state_directory() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME", "")
    base = Path(xdg) if os.path.isabs(xdg) else Path.home() / ".local" / "state"
    return base / "loop-reviewer"
