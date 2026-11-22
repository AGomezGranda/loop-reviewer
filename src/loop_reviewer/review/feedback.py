"""Format the saved feedback for private copy and terminal display."""

from loop_reviewer.review.model import ReviewDraft


def finding_lines(draft: ReviewDraft, *, markdown: bool = False) -> list[str]:
    lines: list[str] = []
    for number, finding in enumerate(draft.ordered_findings(), start=1):
        prefix = f"{number}. "
        indent = " " * len(prefix)
        condition, consequence = finding.condition, finding.consequence
        if markdown:
            condition = _indent_continuations(condition, indent)
            consequence = _indent_continuations(consequence, indent)
        lines.append(f"{prefix}[{finding.severity}] {condition}")
        lines.append(f"{indent}Consequence: {consequence}")
        if finding.location is not None:
            lines.append(
                f"{indent}Location: {finding.location.path}:{finding.location.right_line}"
            )
    return lines


def _indent_continuations(text: str, indent: str) -> str:
    first, *remaining = text.split("\n")
    return "\n".join(
        [first, *(f"{indent}{line}" if line else "" for line in remaining)]
    )


def feedback_markdown(draft: ReviewDraft) -> str:
    findings = finding_lines(draft, markdown=True)
    return (
        "\n".join([draft.summary, "", "## Findings", "", *findings])
        if findings
        else draft.summary
    )
