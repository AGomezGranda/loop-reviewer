# Local data and upgrades

## What leaves your machine

Generating a new draft fetches the pinned comparison and captures discussion from GitHub. Analysis is local by default. With explicit [model consent](configuration.md#model-consent-and-budget), a qualified provider may receive selected pinned source, but not GitHub tokens or discussion. The saved report may contain private source excerpts and leads, but not raw model output or discussion bodies. Generating, editing, copying, or finishing a draft does not post it to GitHub. A [confirmed publication or an explicit resend](publishing.md) is what submits the previewed review.

Git fetch caps each pack at 256 MiB. Intake verifies both pins and opens a private object view for bounded analysis without a checkout or a whole-patch requirement. Loop-reviewer attempts to remove them on normal exit and failure and warns if cleanup fails. A hard kill can leave scratch data in the OS temporary directory without a warning.

## Where state lives

The CLI and browser share `reviews.sqlite3` under `$XDG_STATE_HOME/loop-reviewer/`. When `XDG_STATE_HOME` is unset or relative, the default is `~/.local/state/loop-reviewer/`. The database holds drafts, completion history, publication attempts and receipts, and the verified inbox settings and cached scan data. When a draft has a versioned reviewer report, the database retains the original separately from editable feedback. Copy and publication previews use only editable feedback. The [TOML configuration file](configuration.md) holds editable settings.

The local OS account is the trust boundary. Switching `gh` accounts doesn’t hide saved data in the workspace or a draft addressed by its run ID. `draft show` displays the account and revision recorded on the draft. The browser server listens on an ephemeral loopback port; keep its launch link private and don’t expose the workspace to other users.

Keep the state directory on a local filesystem on macOS or Linux for process locking. The selection-only `open` command doesn’t need these locks. The `inbox-watch/` subdirectory is disposable polling metadata. Remove it only after all browser servers, terminal watchers, and scans have stopped.

Each workspace server shares two review workers and up to 32 waiting launches across its tabs. Refreshing or closing a tab preserves the queue and recent launch notices while the server is running. A graceful shutdown drops waiting launches and lets started reviews finish. On restart, **Reviews** reads saved attempts and drafts from SQLite. An attempt whose process lock has cleared after an interruption requires an explicit retry. The CLI and browser share the per-comparison lock.

## Repository renames

Reviews saved with a stable GitHub repository ID can be reopened at the new URL after a rename. Legacy records saved with only a repository name aren’t matched by URL: the old name may now belong to another repository. Legacy drafts remain reachable by run ID until completion, and repeating `draft finish` with the original run ID and version still shows an existing completion.

## Back up before upgrading

Stop all processes that can write state, including browser workspaces and terminal watchers, before backing up `reviews.sqlite3`. The current schema is v13. Upgrading preserves existing drafts, exact publication previews, completion history, and inbox data. Older releases cannot open the upgraded database.

Stats uses the time Loop Reviewer first records a local completion or a confirmed GitHub publication, including when confirmation happens during recovery. Completions saved before v13 have no recorded date: they count in all-time totals but not in recent counts or weekly activity.

Prefer rolling forward. If you need to roll back, first [recover unresolved publication attempts](publishing.md#if-you-dont-know-whether-github-received-it) and preserve the upgraded database and its receipts separately. Restoring a pre-upgrade backup does not undo GitHub reviews or include publication records created since the backup. Reconcile the restored database with the preserved state and GitHub before enabling publication again.
