# Set up and use the local inbox

The inbox is optional. Use a [PR URL directly](reviewing.md#open-or-resume-a-private-draft) to review outside your watch list without changing discovery settings.

## Choose repositories

Edit the [configuration file](configuration.md) or open **Setup** in the browser. Enter one shared Jira project key and the first repository URL, then select **Add repository** for each additional URL. You can also paste one URL per line into a repository field. Select **Save settings**.

In the CLI, specify the project key once and repeat `--repo` for each repository:

```sh
loop-reviewer inbox setup --jira-project ABC \
  --repo https://github.com/ORG/REPO \
  --repo https://github.com/ORG/OTHER_REPO
loop-reviewer scan
```

Browser and CLI setup write to the same TOML file after `gh` can list PRs in every repository. Edits made directly to the file are verified when the inbox is next read or scanned. Setup records the authenticated account for each host and each repository’s stable ID. If you switch GitHub accounts, sign back in as the configured account or run setup again for the intended account. If an old repository URL no longer resolves after a rename, rerun setup with its current URL.

## Understand what appears

A PR must be open and non-draft, and at least one of these must apply:

- Your review was requested directly.
- You authored it.
- Its title starts with a bracketed ticket for the configured Jira project, such as `[ABC-123]`. Setup normalizes the key to uppercase. The title must match the key exactly and use ASCII digits for the issue number.

Both queues show the most recently opened PRs first across watched repositories. In the browser, select **Newest first** to switch to **Oldest first**, or **Requested from me** to show only direct requests. Repository counts follow the selected queue and request filter. Hover over the opening age or focus the PR link to see the exact opening time.

In **Setup**, enter one word or phrase per line under **Exclude PR titles containing**. A match anywhere in the title hides the PR from both queues, including direct review requests. Matching ignores capitalization. Clear the field and save to show those PRs again.

Completed initial reviews stay out of the inbox, even after a push. **Last saved** describes local review history; it doesn’t tell you whether the draft matches the current revision or a review process is still running. Each repository shows its last successful scan and any current error. Cached rows are marked stale when a refresh fails, or hidden if repository access or identity can’t be verified.

## Defer or start a review

Move a PR to **Later** to stop watch nudges without losing the row. Deferral survives restarts, pushes, and temporary ineligibility. Restore a row to bring it back to **Active**. You can open a review from either section.

```sh
loop-reviewer inbox later PR_URL
loop-reviewer inbox restore PR_URL
loop-reviewer inbox open PR_URL
```

`later` and `restore` work offline on cached rows. `inbox open` verifies the account and repository, then selects the current comparison instead of trusting cached PR metadata.

## Scan now or watch

Use **Scan now** in the browser or `loop-reviewer scan` in the CLI to refresh on demand. Both wait for any active scan. The CLI exits `0` when all checks succeed, `1` if it can’t produce a fully successful report (for example, a repository failed, the scan was superseded, setup is missing, or state couldn’t be read), or `130` when interrupted.

Open the browser workspace or run `loop-reviewer watch` for scheduled scans. Browser tabs and terminal watchers share a two-minute schedule. The first watcher makes discovery due immediately, slow scans skip missed ticks, and scheduled checks stop when the last watcher leaves. The CLI watcher shows repository failures without stopping, nudges only for new or changed active non-stale rows, and shows a deferred count. A superseded scan retries at the next tick. `watch` exits `1` for missing setup or a local state error, and `130` on Ctrl-C.

Setup, scan, and watch do not run the reviewer or publish feedback.
