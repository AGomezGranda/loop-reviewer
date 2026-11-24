# Local reviewer replay

This authored corpus freezes the inputs for a later targeted-context experiment. It does not measure model quality, spend, or detection. The preparation command needs no credentials, model, or hosted fetch:

```sh
mise exec -- uv run --locked python -m tests.support.reviewer_replay --output .local/reviewer-replay
```

The command stages six local SHA-1 repositories and verifies the pins, inventory counts, input digests, and legacy eligibility against the checked-in [manifest](../tests/fixtures/reviewer_replay/manifest.json) before publishing the output. Repeating the command verifies a matching output directory without replacing it. If an existing copy differs, choose a new output directory. The generated `repos/<case>/` directories retain both pinned revisions (`base` and `main`).

Each `diff-only/<case>.json` contains the pinned comparison, a bounded scan of changed paths, and whole hunks in path-round-robin order. `file` references the zero-based `files` index. `omissions` records skipped hunks and failed per-path reads; `omitted_hunks: null` means the count is unknown. `omitted_paths` counts rows whose path metadata did not fit. All fields, including metadata and omissions, count toward the 64 KiB UTF-8 JSON limit. SHA-256 digests are over exact payload bytes, including the final LF. No caller excerpts, compiler output, discussion, case name, split, or answer labels appear in this payload.

`legacy.json` records `eligible`, `unsupported_size`, `unsupported_encoding`, or `unsupported_binary`. Only eligible whole patches are saved to `legacy/<case>.patch`; the 128 KiB ceiling and eligibility checks match the hosted diff input. Unsupported cases are not zero-detection results.

## Frozen v3 baseline and scoring protocol

The manifest records the split, full pins, expected inventory count and changed text lines, the bounded input digest, and the diff arm's retrieval charge. The charge includes hunks omitted from the artifact and reserves the per-read cap when the adapter cannot report bytes for a failed read. The separate [labels](../tests/fixtures/reviewer_replay/labels.json) give the expected defect IDs, cause, trigger, consequence, impact, and pinned source ranges. Negative cases have an empty defect list. Keep labels and split metadata out of prompts and tool responses. Tune only on development cases; hold out the consumer adaptation and the 100-file, 5,000-line churn case.

1. Compare **bounded diff-only** against **targeted evidence** on the same model and model settings, strict result schema, and per-case ceilings. Start both arms from the same changed-file inventory. Count retrieved bytes (including material not ultimately shown) and serialized prompt bytes toward the same combined input allowance. Use at most 64 KiB of serialized evidence, 80 KiB of prompt input, 160 KiB of combined retrieval and prompt input, and 16 KiB of output per case per arm. Reserve at most 250,000 USD micros per case per arm before dispatch; tighten the common ceiling if necessary for the selected model, and record the actual locked configuration before tuning. Do not send a request whose worst-case charge cannot be reserved. Report legacy Pi separately where eligible if its spend cannot be matched.
2. Do not inject deterministic compiler findings into either model arm or score them as model detections. Report local-only findings on their own. Adjudicate model findings against the pinned evidence and label a match only when the cause, trigger, and consequence identify the same defect. Record distinct verified defects, misses, false positives, and duplicate reports separately. Also record partial and unsupported coverage, token and spend usage, and elapsed time; show the largest-case results separately.
3. Require at least **80% precision** over adjudicated distinct findings and no more than **one false finding on any PR**. Zero findings do not establish precision or improvement. Adding context must improve consequential detection at comparable spend, or preserve detection at lower spend.

These crafted cases check behavior, not external validity. Before claiming a production quality gain, validate on independently labeled historical PRs.

### Pilot status

No live pilot has run. The historical OpenAI snapshot `gpt-5-nano-2025-08-07` is unqualified and retained only for offline fixture accounting. It is not a fallback. The [model documentation](https://developers.openai.com/api/docs/models/gpt-5-nano) lists a 400,000-token context window (272,000 accepted input tokens) and uncached standard text rates of $0.05 per million input tokens and $0.40 per million output tokens. The offline quote conservatively reserves against the full context and 2,048 completion tokens, or 20,820 USD micros per request. The [deprecation schedule](https://developers.openai.com/api/docs/deprecations) lists a December 11, 2026 shutdown. None of these documentation checks establish live availability, schema acceptance, or actual billing.

The frozen `generated-churn` diff exceeds the combined allowance on retrieval alone. Its artifact also exceeds the protected investigation prompt allowance once JSON request framing is included. Preserve its digest and report the inability to dispatch as `work_limit`, not a miss. The precision and comparative detection gates have not been measured; no quality gain is claimed.

### Offline fixture runs

`tests.support.score_reviewer` can exercise both arms against fixed responses without contacting a provider. Supply one `<case>.json` per selected split in the response directory, with `diff` and `targeted` arrays of attempt records. An ordinary attempt is `{"reply": { ... }}` using the strict investigation or verification reply. Fixture responses may also supply `usage`, `finish_reason`, `refusal`, `raw_content`, or `transport_error: "timeout"`. These are test results, not measured model performance or cost.

```sh
mise exec -- uv run --locked python -m tests.support.score_reviewer \
  --replay-dir .local/reviewer-replay --output .local/reviewer-scores-dev \
  --split development --fixture-responses /path/to/fixture-responses
```

Development writes the configuration lock beside the frozen inputs; use that same replay directory and configuration for holdout. The scorer records per-arm omissions, the paths both arms explicitly reviewed, and compiler findings separately. `--live` records the unqualified profile without sending requests. Do not interpret `unsupported_model`, a work-limited run with no reviewed paths, or fixture responses as evidence of a quality gain.

After inspecting the pinned source, rerun the scorer with `--adjudications FILE` in place of the response flag to score the saved `runs.json` offline. The file is a JSON array of entries with `case`, `arm`, `candidate_id`, and `verdict`. Use `match` or `duplicate` with a label ID (`label`), or `false_finding` with a pinned-source `rationale`.

## V5 hosted-cutover replay

Prepare a separate corpus with `python -m tests.support.reviewer_replay --policy 5 --output .local/reviewer-replay-v5`. This keeps the frozen baseline and adds four mixed-language cases. Pass `--policy 5` to the scorer for both the bounded diff and targeted arms. Lock settings on development before running holdout. Keep compiler detections separate from model detections.

`--legacy-results FILE` accepts `LegacyReplayV1`: a JSON object with `schema_version: 1` and `cases`. Each case records `case`, `merge_base`, `head`, `checkout_commit`, `model_id`, `outcome`, and `findings`. The outcome is `reviewed`, `patch_limit`, or `failed`. Findings contain only `condition`, `consequence`, `severity`, and nullable `path` and `line`; do not include raw provider output. Capture the observations from the pre-cutover checkout. The saved replay gives findings in this arm IDs like `l00001` for adjudication with `arm: "legacy"`. Patch-limit cases are unsupported, not false negatives. An unmeasured Pi-supported holdout case blocks the gate.

The measured v5 holdout must reach at least 80% distinct precision and at most one false finding per PR. It must retain every adjudicated consequential finding from the supported Pi holdout and either detect more or produce fewer false findings or duplicates. At matched spend the targeted arm must detect more consequential findings than the bounded diff arm, or retain its detections at lower spend. Accounting faults and fixture responses do not qualify. Report the large case separately.

### Candidate profile (not qualified)

The [OpenAI model catalog](https://developers.openai.com/api/docs/models/gpt-5.4-mini) lists `gpt-5.4-mini-2026-03-17` for Chat Completions with Structured Outputs, a 272,000-token input maximum, and standard uncached prices of $0.75 per million input tokens and $4.50 per million output tokens. For qualification, use an 84,000-token accepted input bound, reserve 90,000 input tokens, and cap completion at 2,048 tokens including reasoning. The quote is 76,716 USD micros; three reserved calls cost at most 230,148 USD micros, within the 250,000 replay reserve. The adapter also caps the serialized request at 80 KiB.

No requests have verified those bounds, the live snapshot/tier, either strict schema, or accounting under retries. `QUALIFIED_PROFILE` remains unset. Do not enable it from documentation or fixture results. With explicit paid authorization, qualify using only authored fixtures, record both projected schema digests and the measured results, then run the authored replay. The scorer's v5 `--live` flag authorizes provider calls only once a qualified profile is configured. Hosted drafts now use 2.0 without a provider. The live qualification and replay are still pending; local-only operation does not establish parity with Pi.
