# Review a pull request

Follow the [quickstart](../README.md#quickstart) to install the tool. In these examples, run `loop-reviewer` from the installed environment. From the checkout, prefix the commands with `mise exec -- uv run --locked`; after setup you can also invoke `venv/bin/loop-reviewer` by absolute path from another directory.

## Open or resume a private draft

**Browser.** Run `loop-reviewer ui`, paste a PR URL, and select **Open review**, or launch from an Inbox row. Continue using the workspace while the review runs. Select **Reviews** to see its status, then **Open draft** when it’s ready. If the browser doesn’t open automatically, use the launch link printed in the terminal. The session survives a page refresh. Keep the launch link private because it grants access to the local workspace.

**CLI:** Replace the sample URL with an open, non-draft PR you can access:

```sh
PR_URL=https://github.com/OWNER/REPO/pull/123
loop-reviewer open "$PR_URL"    # Inspect the selected revision without generating a draft.
loop-reviewer review "$PR_URL"  # Generate or resume a private draft.
```

Neither operation posts to GitHub. You don’t need an [inbox](inbox.md) to review a PR by URL. Selecting or reviewing uses the standard GitHub API endpoint for the PR host, not custom `gh` `api_host` overrides. Each selection uses a single captured GitHub credential.

A draft is pinned to the target branch, merge base, and head commit. Reopening the same current comparison under the same GitHub account resumes the saved draft without rerunning analysis or checking model configuration, but still needs access to the PR on GitHub. If the PR advances before completion, reopening can start a separate draft for the new comparison. The old draft remains pinned to its revision and accessible in **Reviews** or by run ID.

Use **Retry review** in **Reviews**, or `loop-reviewer review --retry "$PR_URL"`, to replace a failed or interrupted attempt for the same comparison. If the comparison changed, submit the PR again to review the current revision. If the CLI says it saved a draft but couldn’t check history, use the printed `draft show RUN_ID` command to check its status.

## Edit or copy feedback

In the browser, edit the draft before you choose **Copy feedback**, finish locally, or [preview a publication](publishing.md). In the CLI, use the run ID and draft version shown in the preview. For example, for run `42` starting at version `1`:

```sh
loop-reviewer draft show 42
loop-reviewer draft edit-summary 42 --version 1 --file summary.txt
loop-reviewer draft edit-finding 42 1 --version 2 --condition-file condition.txt
loop-reviewer draft drop-finding 42 2 --version 3
loop-reviewer draft copy 42
```

Each successful edit prints the saved preview and increments its version. Finding numbers are positions in that preview; use the latest numbering and version. If a command reports a stale version, run `draft show` again before retrying.

The CLI’s edit inputs must be regular UTF-8 files of at most 64 KiB per field. It accepts LF and CRLF and saves LF. Keep the files private and delete them when no longer needed. `draft copy` writes Markdown to stdout, not to your clipboard; pipe or redirect it only to a destination you trust. Draft commands work offline without `gh` or model access.

New drafts use report v6: the reviewer inspects pinned text and independently verifies candidate findings against cited source, without running a compiler. With no model available or no spending allowance, eligible files remain unfinished and the report has no behavioral findings. Available discussion references can still appear privately. Older saved reports, including v5 compiler findings, remain unchanged when reopened.

The original report stays private and unchanged as you edit. A partial report lists unfinished coverage; “No verified findings in the reviewed scope” is not a clean bill of health. A complete report means the required checks finished, not that the PR is safe or approved. Drafts saved before original reports were introduced have no original report.

## Finish privately or publish

Copy any feedback you want to take elsewhere **before** finishing. To record a private completion, select **Finish locally** or run `loop-reviewer draft finish 42 --version 4` after the example edits. This never posts to GitHub and can finish an older pinned draft. If you want to send feedback to GitHub instead, [preview and confirm a publication](publishing.md) without finishing locally first.

A local finish or successful publication completes the initial review for the whole PR under the saved GitHub account. Other drafts for that PR and account are no longer editable or available via `draft show` or `draft copy`. A later push, retarget, or closure does not start another initial review, even with `--retry`. Follow-up verification of earlier findings is not implemented.

Use **History** in the browser to see the reviewed revision and final feedback, with a GitHub receipt if published. In the CLI, run `loop-reviewer review "$PR_URL"` again; it shows completion history instead of generating a draft. This needs readable PR metadata under the same account, but no model access.
