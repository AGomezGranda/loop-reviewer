# Configure Loop-reviewer

Use **Setup** in the browser or create `~/.config/loop-reviewer/config.toml`.
If `XDG_CONFIG_HOME` is an absolute path, Loop-reviewer uses
`$XDG_CONFIG_HOME/loop-reviewer/config.toml` instead.

```toml
[inbox]
jira_project_key = "ABC"
excluded_title_terms = ["translation"] # Optional, case-insensitive title matches
repositories = [
    "https://github.com/ORG/REPO",
    "https://github.com/ORG/OTHER_REPO",
]
```

Inbox setup is optional. It takes one shared Jira project key, such as `ABC`
for PR titles that start with `[ABC-123]`, and one or more HTTPS repository
URLs. Each title exclusion is a single-line word or phrase. Legacy `[reviewer]`
values are ignored and preserved when saving.

## Edit from the browser, CLI, or file

**Setup** saves to the same file and picks up manual edits within a few
seconds while the tab is in the foreground. If the file changes while you
have unsaved browser edits, reload the latest settings before saving.
`loop-reviewer inbox setup` also updates the file. Manual file edits apply to the inbox on its next read or scan.
Use **Scan now** to refresh its PR list immediately.

Loop-reviewer verifies repository access with `gh` before replacing the
verified watch list in SQLite. If verification fails, correct the settings in
**Setup** or in the file. A manual file edit that would switch GitHub accounts
or replace the identity of a previously watched repository at the same URL
requires an explicit save in **Setup** or `loop-reviewer inbox setup`. Changing
only title exclusions does not reverify or rebind the watch list; save again
without changing exclusions if you intend to reverify it.

Existing inboxes keep using the settings in SQLite until you save the inbox
in **Setup**, rerun CLI setup, or add an `[inbox]` section. An absent
`[inbox]` section leaves the last verified watch list in place.

## Model consent and budget

The CLI and browser run the local reviewer on pinned Git data. To enable AI
investigation and verification, put Pi 1.7.0 on PATH, authenticate the desired
provider in Pi, and set both variables before launching the CLI or web server:

```sh
export LOOP_REVIEWER_PI_MODEL='provider/exact-model-id'
export LOOP_REVIEWER_MAX_MODEL_SPEND_USD_MICROS=500000
loop-reviewer check-review-model
```

Use the provider and model ID from your Pi catalog. The spend allowance is in
millionths of a US dollar ($0.50 above), from zero (the default) through $3.
These settings authorize sending selected pinned excerpts to that Pi provider.
Pi can use its saved credentials or the selected OpenAI or Anthropic API key.
For standalone v5/v6 reviews, use the same model selector and pass
`--allow-provider --max-model-spend-usd-micros 500000` instead of the budget variable.

Each investigation and verification uses a fresh Pi process in an empty
directory. The bundled extension makes one text request, with tools, project
instructions, session storage and provider retries disabled. The adapter supports
Pi's OpenAI Completions, OpenAI Responses (including Codex), and Anthropic Messages protocols with
a bounded completion and positive catalog prices. Keep prices and context limits
accurate for custom endpoints. The reviewer quotes the serialized prompt in UTF-8
bytes plus a margin for provider framing, and reserves up to 2,048 output tokens
per call. The configured model's context window limits what can be sent, not
what is reserved. Investigation also protects time and budget for verification.

`check-review-model` checks the catalog, configured credentials, and the budget
floor without sending a provider request. It cannot validate credentials with the
provider or guarantee that a particular PR fits the allowance. Run it in the same
environment you use to launch the browser workspace; restart the workspace after
changing settings.

Zero allowance keeps the review local. Invalid model setup is reported when starting
a new hosted review; an allowance too small for the actual comparison leaves AI
coverage unfinished. Resuming a saved draft does not contact Pi or rerun analysis.
Use **Re-run analysis** in Reviews or `loop-reviewer review --retry PR_URL` for a
partial draft. The historical replay tool has its own frozen model configuration.
