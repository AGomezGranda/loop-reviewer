# Use a Python coordinator and CLI with a React UI

The CLI and the browser UI share a Python review coordinator and a local SQLite store. The browser UI uses TypeScript, React, and shadcn/ui with Node.js tooling. The coordinator owns discovery eligibility, draft and publication decisions, revision checks and finding verification so both interfaces apply the same rules.
