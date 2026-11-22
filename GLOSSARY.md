# Local PR review

Developers choose a pull request, prepare a private review and decide which feedback to share.

## Language

**Watched repository** — A GitHub repository a developer has selected for pull request discovery.

**Jira project key** — The project identifier at the start of a ticket, such as `ABC` in `ABC-1234`. Each developer chooses one key for all their watched repositories.

**Eligible pull request** — An open, non-draft pull request in a watched repository that the developer authored or was directly asked to review, or that has a title starting with a bracketed ticket for the developer’s configured Jira project, such as `[ABC-1234]`. Eligibility determines which PRs discovery adds to the inbox.

**One-off review** — A review the developer starts by supplying a PR URL independently of their discovery settings.

**Reviewed revision** — The PR comparison the developer selected, identified by its target branch, merge base and head commit.

**Review verdict** — The reviewer’s recommendation for the examined revision, either `approved` or `comment`.

**Finding** — Feedback describing a problem in the reviewed revision, the conditions under which it occurs and its consequence. A finding can refer to a particular line of the diff.

**Review draft** — The developer’s private, editable summary and findings for a reviewed revision, accompanied by the reviewer’s recommended verdict.

**Inbox** — The developer’s collection of discovered PRs awaiting attention. Choosing “Later” keeps a PR available there and silences repeat prompts for it.

**Completed review** — A review the developer finished locally or successfully published to GitHub, associated with its reviewed revision and final feedback.

**Finding verification** — A developer-requested check of a completed review’s retained findings against a newer revision of the same PR.
