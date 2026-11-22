# Local data and upgrades

## What leaves your machine

Generating a new draft sends the pinned Git diff and revision identifiers to the configured Pi provider. Use only repositories approved for that provider. Loop-reviewer does not forward GitHub tokens to Pi, and it saves structured feedback rather than raw diffs or raw model output. Generating, editing, copying, or finishing a draft does not post it to GitHub. A [confirmed publication or an explicit resend](publishing.md) is what submits the previewed review.

Diff preparation uses Git and caps each fetch pack at 256 MiB and the generated UTF-8 patch at 128 KiB. Diff preparation and Pi use private temporary directories. Loop-reviewer attempts to remove them on normal exit and failure and warns if cleanup fails. A hard kill can leave scratch data in the OS temporary directory without a warning.

## Where state lives

The CLI and browser share `reviews.sqlite3` under `$XDG_STATE_HOME/loop-reviewer/`. When `XDG_STATE_HOME` is unset or relative, the default is `~/.local/state/loop-reviewer/`. The database holds drafts, completion history, publication attempts and receipts, and inbox settings and cached scan data.

The local OS account is the trust boundary. Switching `gh` accounts doesn’t hide saved data in the workspace or a draft addressed by its run ID. `draft show` displays the account and revision recorded on the draft. The browser server listens on an ephemeral loopback port; keep its launch link private and don’t expose the workspace to other users.

Keep the state directory on a local filesystem on macOS or Linux for process locking. The selection-only `open` command doesn’t need these locks. The `inbox-watch/` subdirectory is disposable polling metadata. Remove it only after all browser servers, terminal watchers, and scans have stopped.

## Repository renames

Reviews saved with a stable GitHub repository ID can be reopened at the new URL after a rename. Legacy records saved with only a repository name aren’t matched by URL: the old name may now belong to another repository. Legacy drafts remain reachable by run ID until completion, and repeating `draft finish` with the original run ID and version still shows an existing completion.

## Back up before upgrading

Stop all processes that can write state, including browser workspaces and terminal watchers, before backing up `reviews.sqlite3`. The current schema is v8. Upgrading to v8 preserves existing inbox settings and scan data; earlier releases that use an older schema cannot open the upgraded database.

Prefer rolling forward. If you need to roll back, first [recover unresolved publication attempts](publishing.md#if-you-dont-know-whether-github-received-it) and preserve the upgraded database and its receipts separately. Restoring a pre-upgrade backup does not undo GitHub reviews or include publication records created since the backup. Reconcile the restored database with the preserved state and GitHub before enabling publication again.
