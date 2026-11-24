# Configure Loop-reviewer

Use **Setup** in the browser or create `~/.config/loop-reviewer/config.toml`.
If `XDG_CONFIG_HOME` is an absolute path, Loop-reviewer uses
`$XDG_CONFIG_HOME/loop-reviewer/config.toml` instead.

```toml
[inbox]
jira_project_key = "ABC"
repositories = [
    "https://github.com/ORG/REPO",
    "https://github.com/ORG/OTHER_REPO",
]
```

Inbox setup is optional. It takes one shared Jira project key, such as `ABC`
for PR titles that start with `[ABC-123]`, and one or more HTTPS repository
URLs. Legacy `[reviewer]` values are ignored and preserved when saving.

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
requires an explicit save in **Setup** or `loop-reviewer inbox setup`.

Existing inboxes keep using the settings in SQLite until you save the inbox
in **Setup**, rerun CLI setup, or add an `[inbox]` section. An absent
`[inbox]` section leaves the last verified watch list in place.

## Model consent and budget

Analysis runs locally by default. `LOOP_REVIEWER_MAX_MODEL_SPEND_USD_MICROS`
accepts an integer from `0` (the default) through `3000000` ($3) in the
process environment at launch. A positive allowance explicitly authorizes
sending selected pinned source to OpenAI using `OPENAI_API_KEY` if an exact
provider profile is qualified. Set the key in the environment, not in TOML.

No profile is currently qualified. A positive allowance without a qualified
profile or key still runs the same reviewer locally and reports the missing
model coverage. There is no provider fallback. Resuming a saved draft does
not consult the allowance or rerun analysis.
