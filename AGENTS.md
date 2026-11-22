# loop-reviewer

Loop-reviewer is a local GitHub PR review tool. The Python CLI and the FastAPI-served React workspace share drafts, inbox settings and scans, and review history through local SQLite state. GitHub reads and publication use `gh` credentials, Git prepares the selected comparison, and Pi generates the private draft. Follow-up verification of earlier findings is planned in ADR 0002 and is still pending.

Use the [README quickstart](README.md#quickstart) for runtime setup, the [development guide](docs/development.md) for commands and packaging, the [glossary](GLOSSARY.md) for workflow terms, and the [ADRs](docs/adr/) for design decisions.

## Architecture

- In `src/loop_reviewer/`, keep review and inbox rules in `review/` and `inbox/`. Inbox can use review contracts. Keep dependency direction acyclic and inject I/O, clocks, and ID generation through workflow contracts. Implement GitHub, Git, Pi, SQLite, and process locking in `integrations/`.
- Wire shared operations in `composition.py`. Keep `cli/` and `local_http/` focused on input, workflow dispatch, and output so both interfaces apply the same rules. Python import boundaries are checked by Import Linter and Ruff.
- In `web/src/`, use `app/` for routing and cross-feature coordination, `features/` for screens and their queries, and `components/`, `api/`, and `lib/` for shared code. Dependencies between features go through routes. Dependency Cruiser checks these boundaries.
- Treat FastAPI routes and models as the source for the browser API contract. Regenerate `web/src/api/schema.d.ts` after HTTP contract changes with `mise exec -- pnpm --dir web api:generate`. Use the shared client in `web/src/api/client.ts` for browser requests and preserve the loopback session, bearer capability, and mutation origin checks.

## Workflow invariants

- Pin a review to the selected target branch, merge base, and head. Preserve stale drafts locally and recheck the comparison before publishing or retrying. Submit the reviewed head explicitly to GitHub.
- Completing locally or successfully publishing completes the initial review for that GitHub account and PR. Preserve the completed revision in history and keep completed initial reviews out of discovery across pushes.
- Require confirmation of the exact saved publication preview. An uncertain send blocks edits and local completion, and recovery checks GitHub before any resend. Follow the [publication guide](docs/publishing.md) for retry rules.
- Preserve shared state across CLI and browser runs. Keep SQLite migrations and process locks in `integrations/local_state/`. Use stable GitHub repository IDs and verify the configured account and current comparison when opening a cached inbox row. See [local state](docs/local-state.md) and [inbox behavior](docs/inbox.md).

## Development

- Use `mise` for the pinned Python, uv, Node.js and pnpm versions. From the repository root, run `mise trust` once to trust the project configuration, then `mise install` and `mise run setup`.
- Manage Python dependencies at the repository root with `mise exec -- uv`. Keep `uv.lock` current. Use Pyrefly's strict preset for type checking and Ruff for formatting.
- Manage React and TypeScript dependencies with `mise exec -- pnpm --dir web`. Keep `web/pnpm-lock.yaml` current. Use Biome for linting and formatting and TypeScript for type checking.
- Run `mise run check` and `mise run test` before handing off code changes. Run `mise run format` to apply formatting. For web changes, also run `mise exec -- pnpm --dir web build`.
- Use `mise run web` to build and launch the local workspace. Use `mise run package` to build distributions with the web bundle.

## Web design

- Use shadcn/ui for shared UI primitives. Reuse components in `web/src/components/ui` and add new ones with `mise exec -- pnpm --dir web exec shadcn add <component>`.
- Use the tactical theme in `web/src/index.css`. Prefer charcoal surfaces, compact spacing, thin borders, and the teal accent for primary actions and keyboard focus. Use semantic color tokens so changes apply across components.
- Use monospace for commands, revisions, and identifiers. Pair status colors with readable labels, keep focus visible, and let dense layouts adapt to narrow screens.

## Testing

- Prefer component, behavior and contract tests through public entry points such as the CLI, the coordinator, the local HTTP API and the UI. Assert visible outcomes, persisted state and side effects at those boundaries.
- Parametrize cases that exercise the same behavior and give each case a descriptive ID. Cover applicable edge cases such as invalid input, stale revisions, retries, concurrent operations and partial failures.
- Use temporary SQLite databases for persistence behavior and fakes at external boundaries such as GitHub and the reviewer. Keep full end-to-end tests for essential workflows and focused unit tests for logic that benefits from isolated coverage.
- Python tests live in `tests/`, Vitest component tests sit alongside web code in `web/src/`, and Playwright covers shared CLI and browser workflows in `web/e2e/`. Run `mise run test:install-browser` once to install Chromium, then `mise run test:e2e` to build and run the browser tests, as CI does.
