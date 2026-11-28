# loop-reviewer

Loop-reviewer turns a GitHub pull request into a private, editable review draft. Keep, edit, or drop findings, then copy the feedback for your own fixes, finish privately, or explicitly publish a comment or approval under your GitHub account. The CLI and browser workspace share the same local drafts, inbox, and history.

## Quickstart

Use macOS or Linux with a local filesystem. Install `mise`, Git 2.43 or newer, and the GitHub CLI (`gh`).

From the repository checkout:

```sh
mise trust
mise install
mise run setup
```

Sign in with `gh auth login` if needed (add `--hostname HOST` for the PR’s GitHub host). Then open the browser workspace or review a PR from the terminal.

```sh
# Browser: paste a PR URL and select Open review.
mise exec -- uv run --locked loop-reviewer ui

# Or generate a private draft in the terminal.
mise exec -- uv run --locked loop-reviewer review https://github.com/OWNER/REPO/pull/123
```

Inbox setup is optional. By default, new drafts use pinned Git data without a model and report unfinished behavioral coverage. To enable model-backed investigation and verification for either interface, follow [model setup](docs/configuration.md#model-consent-and-budget) and run `loop-reviewer check-review-model` before starting a review. Generating a draft does not post to GitHub. For the CLI examples in the guides, use the same `mise exec -- uv run --locked` prefix if `loop-reviewer` isn’t on your `PATH`.

In the browser, submit from the Inbox or the PR URL form, then open **Reviews** to track progress and open saved drafts. Each workspace server runs up to two reviews at a time and queues up to 32 more. Use **Retry review** in Reviews to restart an interrupted attempt for the saved comparison.

## Guides

- [Review a PR](docs/reviewing.md) — open the workspace, resume and edit drafts, or finish privately.
- [Publish and recover](docs/publishing.md) — inspect the exact payload before sending and handle interrupted submissions.
- [Configure Loop-reviewer](docs/configuration.md) — share inbox settings and learn how model consent works.
- [Set up the inbox](docs/inbox.md) — discover PRs, defer work, and share a polling schedule across interfaces.
- [Local data and upgrades](docs/local-state.md) — understand what stays local, where state lives, and how to back it up.
- [Develop and package](docs/development.md) — run checks, build distributions, and find the code behind each workflow.

See the [glossary](GLOSSARY.md) for workflow terms and the [architecture decisions](docs/adr/) for design rationale.
