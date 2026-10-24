# AGENTS.md

Lead engine finds owner-run businesses, proves the decision-maker from each business's own website, writes an opener from quoted facts, gates the lead, and hands campaign-ready leads to the email platform. Every lead has a state machine with a repair cycle.

If you were asked to set lead-engine up for someone, follow [SETUP.md](SETUP.md). The rules below are for changing the code.

## Layout

- `src/lead_engine/store.py`: SQLite state. Stages, leases, attempts, backoff, retirement, events, runs.
- `src/lead_engine/engine.py`: one run. `Engine.step` maps a stage to its step; `Engine.work` turns failures into the repair cycle.
- `src/lead_engine/outcome.py`: what a step returns (`Advance`, `Drop`) or raises (`StepError`, `SharedOutage`).
- `src/lead_engine/verify.py`, `fetch.py`, `html.py`, `evidence.py`, `names.py`: reading sites and proving facts.
- `src/lead_engine/voice.py`, `writer.py`: voice rules, the evidence check for openers, the two writers.
- `src/lead_engine/gate.py`, `handoff.py`, `instantly.py`: the last checks and the only write path to the platform.
- `src/lead_engine/backlog.py`: stranded-lead re-qualification.
- `src/lead_engine/sources/`: Google Places, CSV import, discovery filters.
- `src/lead_engine/placement.py`, `warmup.py`: deliverability checks.
- `src/lead_engine/config.py`: config schema and defaults; `templates/lead-engine.example.toml` mirrors it.
- `src/lead_engine/demo.py` and `demo_data/`: the synthetic demo, also used by the tests.

## Rules

- **No fact without evidence.** Every kept fact has `source_url`, `method` and `quote`, and `evidence.supported` must prove its values and relationships from the stored page snapshot. Accept only the exact whole-text identity, contact and opener templates documented in README. Keep these as a small allowlist; do not add denylist heuristics, partial matching, role inference or free-form phrase slots. Opener facts need an explicit business subject; bare fragments do not qualify. Preserve element boundaries and section headings and check complete claims against the full page rather than an isolated excerpt. Unknown kinds and methods fail. Keep every fetched snapshot, including pages with exclusions or competing contacts, bounded by the fetch size cap, and check the serialized snapshots before checkpointing. The gate and both hand-off modes require a complete validated snapshot manifest and evidence version 3; unprovable local evidence is a drop. A new fact kind needs a check in `supported` and a test.
- **Gate under the same identity policy.** Pass the configured roles to the gate and use verification's canonical name key for merged name variants. Every variant must still be proven against its own snapshot.
- **Keep rendered text complete.** Join inline text, including `q` and `cite`, before matching sentences or addresses; track quotation spans separately. Keep direct parent text as separate blocks without joining across child blocks. Preserve heading provenance and store each block's heading ancestry as a list of complete strings without delimiter joining. Split sentences regardless of capitalization while preserving abbreviations. Collapse schema-name whitespace before validation while preserving the original schema quote. A person may share the prospect's company name. Research, snapshot replay and the gate must all use `host_of` for website host comparisons.
- **Scope identity evidence to the person.** Reconcile visible retirement and other personal disqualifications across pages and schema before selecting a decision-maker. Unrelated staff or office descriptions do not disqualify them. Reject other-business affiliations, negations, past or delegated roles and ambiguous assertions. Unsupported sentences that repeat a proven full name invalidate that person, including extra personal descriptions; independent sentences about someone else do not. Use only individually proven full-name variants for contacts; first-name bindings are not allowed. Schema proof needs an explicit Person with a configured jobTitle and worksFor naming the prospect. Preserve every address occurrence and its full contact element; any ambiguous or conflicting occurrence rejects that address. An address local part alone never proves ownership.
- **Unprovable is a drop; unreadable is a retry.** Return `Drop(reason, detail)` only when the pages were read and the proof is not there. Raise `StepError` for anything transient, including a page that failed to load when nothing was proven without it.
- **Never guess.** No address patterns, no enrichment lookups, no surnames taken from addresses or search snippets, no facts that are not on the business's own pages.
- **Recognize permitted evidence consistently.** A proven full-name heading and a supported company fact do not invalidate a person; ordinary body text still needs a complete allowed assertion. The gate must replay these distinctions from stored heading provenance and the same company-fact templates. Exported role evidence must match the selected role and proven person.
- **Openers restate cited facts only.** `voice.check_opener` must pass before a lead is written and again at the gate. Never weaken the evidence check to get a draft through; change `voice.toml` or the templates instead.
- **The repair cycle owns failures.** Steps return `Advance` or `Drop`, or raise. Only `Engine.work` turns exceptions into attempts. Checkpoint with `store.advance` before the next step starts, keep steps safe to repeat, and don't swallow an exception inside a step.
- **`SharedOutage` is for dependencies every lead needs** (the model API, the platform). It releases the lead without using an attempt and stops the run. Don't raise it for one lead's problem.
- **Never send.** `InstantlyClient._post` allows two paths: `/api/v2/leads/add` and `/api/v2/emails/test`. Don't add writes that launch, pause or edit campaigns or accounts, or that email prospects. The hand-off stays dry run by default and the seed test stays a dry run without `--send`.
- **Paid discovery needs explicit permission.** Default `discovery.allow_paid` to false and require a boolean true before billable Places requests. A key alone is not permission. CSV import does not need this opt-in.
- **External writes need durable intent.** Record hand-off intent in SQLite and seed-send progress in the run file before each request. Never repeat an accepted or unresolved write automatically. Validate complete responses before changing lead states. Resume seed runs with `placement send --send --run FILE`, sending only pending entries with unchanged senders, recipients and content.
- **Protect seed journals throughout updates.** Read and resume only files in the configured placement output directory whose filenames match their tokens. Hold that directory's lock from before loading a run until saving is complete. Strip surrounding whitespace and lowercase seed identifiers consistently in configuration, sends, lookups and placement results.
- **Suppressions fail closed.** The configured CSV must exist and have valid headers and entries. Reload it immediately before every live hand-off batch.
- **Fetch politely.** Keep robots.txt handling, timeouts, the size cap, per-host spacing and the honest user agent. Read only pages on the business's own host.
- **Redirects need checks before requests.** Reject API redirects. Website redirects stay on the business's host and obey robots.txt and spacing at each hop. Never retain URL credentials, query strings or fragments in diagnostics, page URLs or persisted lead websites. Sanitize exported and transmitted websites too.
- **Secrets** come from environment variables. Errors and logs never include headers, keys or query strings. Model failures report only exception type and valid HTTP status; all client construction failures are sanitized shared outages too, including errors outside SDK exception classes.
- **Demo output is disposable only by request.** Refuse occupied destinations unless they carry the demo marker and `--reset` is explicit. Never reset an unmarked directory.
- Runtime code is standard library only; the Anthropic SDK is an optional extra.

## Development

- Python 3.11+, `uv`. Run `uv run pytest` and `uv run ruff check` before a PR.
- Manual commands that need credentials use `uv run --env-file .env lead-engine ...`; the CLI does not load `.env` itself.
- Add a failing test with each behavior change. Network code is tested with a fake transport; tests never call real APIs or fetch real sites.
- Test data is synthetic: fictional businesses on example.com and example.org, short fake IDs, no real names, addresses or phone numbers.
