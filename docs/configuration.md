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

[reviewer]
engine = "pi"
model = "PROVIDER/MODEL"
```

Both sections are optional. The inbox takes one shared Jira project key, such
as `ABC` for PR titles that start with `[ABC-123]`, and one or more HTTPS
repository URLs. Pi is the supported reviewer and the default engine. Replace
`PROVIDER/MODEL` with the model you configured in Pi. Credentials stay with
`gh` and Pi.

## Edit from the browser, CLI, or file

**Setup** saves to the same file and picks up manual edits within a few
seconds while the tab is in the foreground. If the file changes while you
have unsaved browser edits, reload the latest settings before saving.
`loop-reviewer inbox setup` also updates the file and preserves the reviewer
settings. Manual file edits apply to the inbox on its next read or scan.
Use **Scan now** to refresh its PR list immediately.

Loop-reviewer verifies repository access with `gh` before replacing the
verified watch list in SQLite. If verification fails, correct the settings in
**Setup** or in the file. A manual file edit that would switch GitHub accounts
or replace the identity of a previously watched repository at the same URL
requires an explicit save in **Setup** or `loop-reviewer inbox setup`.

Existing inboxes keep using the settings in SQLite until you save the inbox
in **Setup**, rerun CLI setup, or add an `[inbox]` section. An absent
`[inbox]` section leaves the last verified watch list in place.

## Choose a model

The CLI and browser pick up the model for the next draft generation without
a restart. Set `LOOP_REVIEWER_PI_MODEL=PROVIDER/MODEL` in the environment
that launches the CLI or browser workspace to override the file. The browser
field displays the saved TOML value. Changes to the model apply when Pi
generates a draft. Resuming an existing draft retains its recorded model.
