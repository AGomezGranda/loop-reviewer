# Develop and package loop-reviewer

Follow the [quickstart setup](../README.md#quickstart) for the pinned Python, uv, Node.js, and pnpm versions and locked dependencies. See [AGENTS.md](../AGENTS.md) for tooling, web design, and testing conventions.

## Common commands

Run these from the repository root:

| Task | Command |
| --- | --- |
| Show CLI commands | `mise exec -- uv run --locked loop-reviewer --help` |
| Build the web bundle and launch the workspace | `mise run web` |
| Check formatting, lint, types, boundaries, and the generated API contract | `mise run check` |
| Run Python and web tests | `mise run test` |
| Apply formatting | `mise run format` |
| Build just the web bundle | `mise exec -- pnpm --dir web build` |
| Install Chromium for browser tests (once) | `mise run test:install-browser` |
| Run browser tests | `mise run test:e2e` |
| Regenerate the web API declaration after HTTP contract changes | `mise exec -- pnpm --dir web api:generate` |
| Build the wheel and source distribution with the browser bundle | `mise run package` |

A wheel built with `mise run package` includes the browser workspace assets. It doesn’t need Node.js or the checkout at runtime. If `loop-reviewer ui` reports a missing bundle in the checkout, run `mise run web` to rebuild it and launch the workspace.

## Standalone Git inventory

`loop-reviewer-inventory` needs Python and Git 2.43 or later (`--attr-source`). It does not need GitHub credentials, a model, SQLite state, or a checked-out branch. Use two full SHA-1 or SHA-256 commit IDs from the local object store; the base must be an ancestor of the head. Identical commits produce an empty inventory.

```sh
mise exec -- uv run --locked loop-reviewer-inventory \
  --repo "$review_repo" --merge-base "$review_base" --head "$review_head"
```

The command writes one v1 JSON report to stdout. Exit **0** means complete inventory, **1** means intake failed (the report includes a typed error and no coverage rows), and **2** means invalid command arguments (diagnostic on stderr, no report). Inventory does not inspect behavior or make model calls; the recommendation is `none`.

Paths encode the original Git bytes using uppercase percent escapes, preserving `/` and the unreserved characters `A-Z a-z 0-9 - . _ ~`. The inventory accepts paths up to 4,096 raw bytes. Committed `linguist-generated` markers are evaluated on each side: set or `true` is `marked`, unset or `false` is `unmarked`, and absent or other values are `unknown`. Git’s binary marker and the `oversized` label (a blob over 1 MiB on either side) do not make inventory coverage incomplete.

Intake is limited to 10,000 changes, 8 MiB of stdout per metadata command, and 60 seconds across Git setup and intake. Git uses 50% rename similarity and an exhaustive search limit of 1,000; some renames can appear as an addition and deletion. The Python adapter’s on-demand blob reads have a separate 1 MiB hard cap and a 10-second timeout.

## Standalone Python checks

`loop-reviewer-check` uses the same pins, Git requirements, and local-only setup as inventory. It compiles each supported changed `.py` side with the installed CPython (3.15.2 in the pinned development toolchain), but never executes the resulting code or imports the reviewed project. The diagnostic records the exact interpreter version rather than guessing the repository's target version.

```sh
mise exec -- uv run --locked loop-reviewer-check \
  --repo "$review_repo" --merge-base "$review_base" --head "$review_head"
```

The command writes one v2 JSON report on stdout. Exit **0** means all required checks finished, **1** means intake failed with a typed error, **2** means invalid arguments (no report), and **3** means partial required coverage. Binary files are excluded and unsupported paths stay visible as gaps. `mechanical` means that the compiler investigation finished, not that the file passed compilation or received semantic review. A recommendation of `comment` indicates a proven head-only compiler error on a changed line; partial runs can retain such findings.

Context is separate: excerpts and syntactic references are pinned evidence, not proof of symbol binding or runtime behavior. A limited or unavailable search does not invalidate a completed compiler check, and a search with no matches does not establish that no callers exist. After intake, work is capped at 30 seconds, 250 blob reads, 250 analyzer calls, 200 hunk reads, 40 searches, and 8 MiB of blob data. Model usage is always zero.

## Experimental standalone semantic review

`loop-reviewer-semantic` uses the same local Git pins and runs compiler checks before optional behavior review. It needs no GitHub account or saved state and never executes reviewed code.

```sh
mise exec -- uv run --locked loop-reviewer-semantic \
  --repo "$review_repo" --merge-base "$review_base" --head "$review_head"
```

The command writes one v5 JSON report by default. Exit **0** means complete, **1** is a typed failure, **2** is invalid arguments, and **3** is partial. A partial run retains every inventory row and any verified findings. The v5 behavior investigation covers selected regular UTF-8 text paths regardless of language, with a separate compiler check for Python. `mechanical` means the compiler check finished; it does not imply behavior was covered. Private leads and unfinished paths do not produce a `comment` recommendation on their own.

Pass `--discussion-snapshot "$snapshot"` to include a trusted local capture in v5. To request an older format, pass `--report-version 3` without discussion or `--report-version 4` with a snapshot. The snapshot must match both pinned commits; the command does not check live GitHub ownership or need credentials. Snapshot files may contain confidential comments and replies. Keep them private, and do not commit or publish them. Reports include capture provenance, omissions, and bounded references, never discussion bodies or replies. Incomplete discussion can make the report partial even when all source checks finished.

Use `loop-reviewer open "$pr_url" --discussion-snapshot "$snapshot"` to create that file from GitHub using the configured `gh` account. Use the printed merge base and head with the semantic command. The capture creates a private file at a new path and exits **0** for complete or **3** for partial capture. Unavailable capture exits **1** without creating the snapshot.

A v4 `existing_thread` link replaces a finding only when an open, non-outdated thread with no replies matches the verified producer, caller, and exception. Its original location must resolve to the pinned producer excerpt. The pilot recognizes the explicit three-part claim, for example: `producer.items() returns [] instead of None. Call consumer.first(). consumer.first() raises IndexError.` Related history can be referenced without replacing a finding. An unanchored review or conversation item needs an explicit path immediately before an exact fenced source excerpt to be referenced. Discussion is never sent to the model.

The default model allowance is zero. A positive `--max-model-spend-usd-micros` (maximum `3000000`) requires `--allow-provider`. The CLI reads `OPENAI_API_KEY` only for v5 when a qualified profile is configured and both consent and a positive allowance are present. **No OpenAI profile is qualified, so the command currently makes no provider calls even when opted in.** If a profile is qualified later, the provider would process the submitted pinned source; only opt in if you are authorized to send it. `store=false` is not a data-retention guarantee. Usage would be estimated at standard uncached rates, not an invoice, and unknown billing would hold the full reservation.

The local semantic witness is deliberately narrow: it checks simple direct Python source imports, constant empty-result changes, and a caller's indexing and guards. It does not establish general Python binding or behavior. See [the replay protocol](reviewer-replay.md) for the experimental status.

## Standalone reviewer schemas

The Pydantic models in [`reviewer/`](../src/loop_reviewer/reviewer/) define the standalone reviewer contracts. Call a model's `model_json_schema()` method to export its schema on demand.

## Code map

The Python backend is organized around two workflows. Each declares the capabilities it needs; adapters supply those capabilities.

- [`review/`](../src/loop_reviewer/review/) owns revision selection, draft generation and editing, local completion, publication, recovery, and history. Persistence contracts live in `store.py`.
- [`inbox/`](../src/loop_reviewer/inbox/) owns eligibility, verified setup, scan results, watch notifications, and cached-row actions. It reads review activity through a review contract and opens reviews through an injected callback.
- [`reviewer/`](../src/loop_reviewer/reviewer/) owns standalone pinned-source analysis, its versioned wire contracts, and its own I/O adapters under `reviewer/integrations/`.
- [`integrations/`](../src/loop_reviewer/integrations/) adapts `gh` and hosted Git retrieval for the review workflow. [`local_state/`](../src/loop_reviewer/integrations/local_state/) owns SQLite transactions, record encoding, and process locking.
- [`composition.py`](../src/loop_reviewer/composition.py) assembles the workflows for the [`cli/`](../src/loop_reviewer/cli/) and [`local_http/`](../src/loop_reviewer/local_http/) entry points. The latter maps HTTP requests to workflow operations and serves the browser bundle.
- [`web/src/`](../web/src/) contains the browser screens and the [generated API declaration](../web/src/api/schema.d.ts).

Dependencies point inward: inbox can read review contracts, but review does not depend on inbox. Neither workflow imports the CLI or integrations or performs I/O directly. `mise run check` checks import direction, cycles, and restricted I/O imports; tests check additional boundary contracts and workflow behavior.

See the [architecture decisions](adr/) for the rationale behind these boundaries and workflow rules.
