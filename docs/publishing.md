# Publish or recover a GitHub review

Publishing uses a saved [review draft](reviewing.md), not Pi. Sign in with `gh` as the account listed on the draft. Each publication operation verifies the account using a single captured credential and uses the standard GitHub REST endpoint for the PR host; custom `gh` `api_host` overrides do not apply. Keep the [state directory and locks](local-state.md) on a local filesystem on macOS or Linux.

## Preview, then confirm

Choose **Comment** or **Approve** in the browser and select **Prepare preview**, or create a preview from the CLI. Replace `RUN_ID` and `VERSION` with the values from the saved draft:

```sh
loop-reviewer publish preview RUN_ID --version VERSION --action comment
```

The action is your choice, not the draft’s recommendation. You can’t approve your own PR; publish a comment or [finish locally](reviewing.md#finish-privately-or-publish) instead.

Previewing doesn’t post anything. Inspect the exact review body, inline comments, and the reviewed target branch, merge base, and head. Unverifiable inline locations fall back to the body before you confirm. Use `--summary-only` (or **Summary only** in the browser) to put every retained finding in the body.

Confirm that preview in the browser, or use the preview ID returned by the CLI:

```sh
loop-reviewer publish confirm PREVIEW_ID
```

A draft edit, a different action, or a changed repository destination requires a new preview and confirmation. If the target branch, merge base, or head changed, reopen the PR to review the new comparison. A push after the last check cannot silently change the pinned commit: GitHub may reject the request or record the review on the older commit.

## If you don’t know whether GitHub received it

Open the unresolved publication from **History**, or recover using the same preview ID:

```sh
loop-reviewer publish recover PREVIEW_ID
```

Recovery checks GitHub without sending anything. Each previewed body starts with a `<!-- loop-reviewer:UUID -->` marker. It’s intended to be hidden in rendered Markdown and visible in the raw review body. Recovery looks for the marker and verifies the account, head, action, and body. A dismissed match proves the review was submitted, not that an approval still counts. Check **History** or `loop-reviewer review PR_URL` for the receipt and original reviewed revision.

An unresolved send blocks edits and local completion for that PR and account across draft versions. Choose the next step based on the outcome:

| Outcome | Next step |
| --- | --- |
| Matching review found | No resend is needed. |
| Definite rejection | Use `loop-reviewer publish retry PREVIEW_ID` or create a fresh preview. If GitHub rejected an inline anchor, try a new `--summary-only` preview. |
| Scan completed without finding the marker | The earlier POST may still have succeeded. Only retry if you accept the duplicate risk. `loop-reviewer publish retry PREVIEW_ID --accept-duplicate-risk` scans again and can resend only the same payload. |
| Conflicting evidence or failed scan | Do not resend; recovery cannot authorize a retry on that evidence. |

The marker is not a GitHub idempotency key. A clean scan can’t guarantee that a retry won’t create a duplicate.
