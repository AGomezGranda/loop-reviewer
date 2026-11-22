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

## Code map

The Python backend is organized around two workflows. Each declares the capabilities it needs; adapters supply those capabilities.

- [`review/`](../src/loop_reviewer/review/) owns revision selection, draft generation and editing, local completion, publication, recovery, and history. Persistence contracts live in `store.py`.
- [`inbox/`](../src/loop_reviewer/inbox/) owns eligibility, verified setup, scan results, watch notifications, and cached-row actions. It reads review activity through a review contract and opens reviews through an injected callback.
- [`integrations/`](../src/loop_reviewer/integrations/) adapts `gh`, Git, and Pi. [`local_state/`](../src/loop_reviewer/integrations/local_state/) owns SQLite transactions, record encoding, and process locking.
- [`composition.py`](../src/loop_reviewer/composition.py) assembles the workflows for the [`cli/`](../src/loop_reviewer/cli/) and [`local_http/`](../src/loop_reviewer/local_http/) entry points. The latter maps HTTP requests to workflow operations and serves the browser bundle.
- [`web/src/`](../web/src/) contains the browser screens and the [generated API declaration](../web/src/api/schema.d.ts).

Dependencies point inward: inbox can read review contracts, but review does not depend on inbox. Neither workflow imports the CLI or integrations or performs I/O directly. `mise run check` checks import direction, cycles, and restricted I/O imports; tests check additional boundary contracts and workflow behavior.

See the [architecture decisions](adr/) for the rationale behind these boundaries and workflow rules.
