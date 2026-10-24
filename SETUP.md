# Setting up lead-engine (for a coding agent)

You are helping the owner of a small business set up lead-engine to find and qualify other businesses as prospects. Work through the steps in order, ask before each choice that is theirs to make, and run the check at the end of each step. Never write credentials into `lead-engine.toml`, into the repo or into chat; they go in `.env` (git-ignored) or the environment of the scheduler. Never enable paid discovery, switch the hand-off to live or send a seed test unless the owner explicitly asks.

## 1. Run the demo

```bash
uv run lead-engine demo
```

Walk the owner through the output: three scheduled runs, the repair-cycle lines (retry, CRASH, resumed, repaired, retired), the backlog pass, and the openers with the quote each one is built from. Open `lead-engine-demo/out/ready.csv` to show that every fact carries its source URL and quote. Done when the demo prints "Totals: 5 ready, 4 dropped, 1 retired, 10 leads in all."

Use an empty destination. Rerunning a marked demo requires `uv run lead-engine demo --reset`; `--reset` cannot erase an unmarked directory.

## 2. Decide who to find

Run `uv run lead-engine init` to create `lead-engine.toml`, `voice.toml` and `suppressions.csv`. Copy `.env.example` to `.env`, leaving values blank until each feature is connected. Manual commands below use `uv run --env-file .env` to load it; the CLI does not load `.env` itself. Then ask:

1. **Markets.** Cities or areas as "City, ST" strings.
2. **Keywords.** What the target businesses call themselves on a map listing ("bookkeeping firm", "home inspection"). Every market is searched with every keyword.
3. **Decision-maker roles.** The default list runs from owner and founder down to general manager. Remove roles the owner doesn't want to email.
4. **Fit.** `require_any`: words a fitting business's site should contain. `exclude_any`: signs of a chain or a poor fit (the defaults catch franchise pages).

Verification and the gate use the same configured role list and the exact allowlist in README's evidence rules. Review those forms with the owner before choosing markets: partial biographies, separate name/title cards, first-name contacts and extra personal clauses do not qualify. Schema proof requires a Person with an explicit jobTitle and worksFor naming the prospect. Every contact occurrence must use an individually proven full name, and a line break cannot hide extra text. Opener facts need the documented full sentences, finite specialty/award phrases and the market's exact place name. An unsupported sentence repeating a proven full name makes that person's eligibility unproven. Do not loosen extraction to raise volume. All fetched snapshots are retained; the gate and hand-off reject legacy or incomplete evidence with `incomplete_evidence`. Re-qualify stranded leads from fresh backlog reads. Website credentials, query strings and fragments are removed before persistence, export and hand-off.

Inline formatting preserves complete addresses and sentences, and direct parent text is checked alongside leaf blocks. Schema names use collapsed whitespace while retaining their original quotes. A business named after its owner follows the same identity templates. Host comparisons ignore capitalization, a leading `www.` and ports consistently during research and replay.

Done when `uv run --env-file .env lead-engine check` prints "Config OK" and the number of searches you agreed.

## 3. Voice and suppressions

- **voice.toml.** Ask for phrases they never want in an email, the longest opener they would send, and a reading level. Rewrite the `[templates]` in their words; a template may only restate its fact, so no added numbers, names or claims. Keep more distinct openings per fact kind than `distinct_window` that pass all voice rules with ordinary fact values, including the reading-grade limit. Existing voice files must include `serving` templates and the current `{award}`, `{year}` award placeholders from the example; years-of-experience facts are no longer extracted. If they plan to use the model writer, replace `examples` with lines they have actually written.
- **suppressions.csv.** Export unsubscribes, bounces and do-not-contact entries from their email platform and CRM into the `email` and `domain` columns. A missing or malformed file stops processing. A header-only CSV is the explicit empty list. Live hand-off reloads it before every batch, so keep it current.
- **User agent.** Set `[fetch] user_agent` to name their business with a contact URL, so site owners can see who is reading their pages.

Done when `uv run --env-file .env lead-engine check` shows the voice file as OK and the suppression counts you expect.

## 4. Connect discovery

- **Google Places** (`[discovery] source = "places"`): in a Google Cloud project, enable the Places API (New), create an API key restricted to it, and set a budget alert. Put it in `GOOGLE_PLACES_API_KEY`. Tell the owner that each search is billed at the Text Search Enterprise rate because it asks for websites, and that `cells_per_tick` times `pages_per_search` caps the searches per run. Leave `allow_paid = false` until they explicitly authorize that cost, then set `[discovery] allow_paid = true`. A key alone does not enable billing, and hand-off dry-run mode does not prevent discovery charges.
- **CSV** (`source = "csv"`): if they already have a list, put it in `import.csv` with the columns from `examples/import.csv`.

Done when `uv run --env-file .env lead-engine check` shows "GOOGLE_PLACES_API_KEY set" and "Paid discovery: enabled" after approval, or the CSV as found. CSV import needs no paid-discovery opt-in.

## 5. First run, with the hand-off as a dry run

```bash
uv run --env-file .env lead-engine run
uv run --env-file .env lead-engine status
uv run --env-file .env lead-engine export
```

Review `out/ready.csv` with the owner: for each ready lead, the role quote, the address and its quote, and the opener with its evidence. Read the drop reasons in `status`. If many leads are dropped for a reason the owner disagrees with, adjust `[verify]` (roles, fit terms) or the voice rules. Do not loosen the evidence checks in `src/lead_engine/verify.py` or `voice.py` to let more leads through.

Done when the owner has reviewed at least ten ready leads and agrees they are the right people with the right addresses.

## 6. Schedule the run every 15 minutes

Pick the example for their system in `examples/scheduling/` (launchd on macOS, systemd on Linux, cron anywhere), replace `/path/to/lead-engine` and `/path/to/uv` (`which uv`), and install it. The cron example also schedules a weekly `backlog` and a daily `warmup --strict`.

Done when `uv run --env-file .env lead-engine status` lists runs about 15 minutes apart.

## 7. Hand-off to Instantly: dry run first, live only on request

1. Create an Instantly API v2 key with the `leads:create` scope and put it in `INSTANTLY_API_KEY`. Put the target campaign's ID in `INSTANTLY_CAMPAIGN_ID`.
2. Make sure the campaign's first email uses `{{personalization}}` where the opener belongs. The campaign's schedule, sending limits and copy stay in Instantly; this tool only adds leads.
3. Leave `[handoff] mode = "dry-run"` for a few runs. Compare the files in `out/handoff/` with what the owner expects.
4. Only when the owner explicitly asks, set `mode = "live"`. Nothing else changes: the same leads, the same checks.

An uncertain POST leaves leads ready but blocks automatic resubmission. `status` shows the count awaiting reconciliation. Inspect SQLite's `handoff_writes` records and the platform before manually correcting local state; never clear unresolved records just to retry.

## 8. Deliverability checks

- **Warmup audit:** add the `accounts:read` scope and run `uv run --env-file .env lead-engine warmup`. Go through every flagged inbox with the owner and change settings in Instantly yourself only if they ask. Adjust `[warmup]` ranges if their provider recommends different values.
- **Seed test:** ask for two or more mailboxes they own on different providers (for example one Gmail and one Outlook), create IMAP app passwords, add a `[[placement.seeds]]` block per mailbox with the inbox and spam folder names, and put the credentials in the env vars it names. The seed test lists the sending inboxes (`accounts:read`) and sends through the test-email endpoint (`emails:create`), so the key needs both scopes. Set `[placement] body_file` to their real first email. `uv run --env-file .env lead-engine placement send` shows how many emails would go out. Run it with `--send` only when the owner says to, wait about five minutes, then run `uv run --env-file .env lead-engine placement read` and `uv run --env-file .env lead-engine placement report`. Benching a domain is the owner's decision, made in Instantly.

Seed address identifiers are stripped of surrounding whitespace and lowercased for sends, IMAP lookups and placement results.

If a seed send is interrupted, use `uv run --env-file .env lead-engine placement send --send --run out/placement/run-seed-TOKEN.json` with its saved filename. The command checks that senders, seeds, subject and body still match. Accepted and unresolved entries are skipped; only pending entries are sent. A new run is blocked until the earlier run is complete. Investigate unresolved entries in the platform and seed mailboxes before any manual correction; do not reset them to pending without proving no send occurred.

Use the same configured placement output directory for `placement read` and resume. Each run filename must match its token. Both commands lock that directory before loading the journal and keep it locked until saving finishes; an overlapping command fails without rewriting progress.

## 9. Optional: the model writer

If the owner wants openers written by a model instead of templates: `uv sync --extra llm`, put `ANTHROPIC_API_KEY` in `.env`, and set `[writer] mode = "claude"`. It makes one model call per draft and at most two per attempt; tell them that is the metered cost. The same voice and evidence rules apply to every line it writes.

All model client construction failures, including missing credentials and local certificate or configuration errors, stop the run as a shared outage without consuming lead repairs. Errors show a controlled exception type and HTTP status, excluding provider response text. Numeric suffixes such as `20th` and `20k` need their complete published form in the cited evidence; use plain numbers when only those are proven.

## 10. Stranded leads

If the owner has half-researched leads from another tool, put them in a CSV with the columns from `examples/stranded.csv` and run `uv run --env-file .env lead-engine backlog --import stranded.csv`. Explain the four results (recovered, changed, dropped, inconclusive). Run `uv run --env-file .env lead-engine backlog` again whenever `status` shows retired leads; inconclusive ones are retried on the next pass. A shared dependency outage stops the pass and leaves the remaining leads untouched.
