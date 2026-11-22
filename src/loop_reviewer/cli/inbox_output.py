"""Render inbox reports as terminal-safe text."""

from loop_reviewer.inbox.model import InboxReport, InboxRow
from loop_reviewer.inbox.polling import WatchCycle

_STATUS = {
    "draft_ready": "draft ready",
    "running": "running",
    "failed": "failed",
    "abandoned": "interrupted",
}


def terminal_text(text: str) -> str:
    return "".join(char if char.isprintable() else repr(char)[1:-1] for char in text)


def repository_lines(report: InboxReport) -> list[str]:
    lines: list[str] = []
    for status in report.statuses:
        repo = status.repository
        last = (
            status.last_success.isoformat(timespec="seconds")
            if status.last_success is not None
            else "Never"
        )
        lines.append(
            f"{repo.address.host}/{repo.address.slug} — {repo.account} — {report.settings.prefix_label} — Last successful scan: {last}"
        )
        if status.outcome == "pending":
            lines.append("  Scan in progress or interrupted")
        elif status.outcome == "failed":
            stale = " (showing stale results)" if status.visible_count else ""
            lines.append(
                f"  Scan failed{stale}: {terminal_text(status.error or 'Unknown error')}"
            )
        elif not status.visible_count:
            lines.append("  No eligible PRs awaiting review")
        else:
            lines.append(f"  Eligible PRs awaiting review: {status.visible_count}")
    return lines


def row_lines(row: InboxRow) -> tuple[str, str, str]:
    pr = row.pull_request
    ref = pr.ref
    status = (
        f"Last saved: {_STATUS[row.last_saved]}" if row.last_saved else "Not started"
    )
    stale = "[Stale] " if row.stale else ""
    reasons = "; ".join(row.reasons)
    return (
        f"{stale}{terminal_text(pr.title)} — {ref.owner}/{ref.repo} #{ref.number} by {terminal_text(pr.author)}",
        f"  {status} — {reasons} — {ref.url}",
        f"  loop-reviewer inbox open {ref.url}",
    )


def render_inbox_report(report: InboxReport) -> str:
    lines = repository_lines(report)
    for section, rows in (("Active", report.active), ("Later", report.deferred)):
        if rows:
            lines.extend(("", section))
            for row in rows:
                lines.extend(row_lines(row))
    return "\n".join(lines)


def render_watch_cycle(cycle: WatchCycle) -> str:
    report = cycle.report
    lines = repository_lines(report)
    if cycle.changed:
        lines.extend(("", "New or updated active work"))
        for row in cycle.changed:
            lines.extend(row_lines(row))
    stale_count = sum(row.stale for row in report.active)
    if stale_count:
        lines.append(
            f"Stale active rows: {stale_count}. Run loop-reviewer scan to view them."
        )
    lines.append(
        f"{len(report.deferred)} deferred. Run loop-reviewer scan to see Later."
    )
    return "\n".join(lines)
