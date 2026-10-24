# Lead engine

Lead engine finds owner-run small businesses in the markets you choose, proves who decides at each one from the business's own website, writes a short personal opener from facts it can quote, checks every lead against your voice rules and suppression list, and hands the campaign-ready leads to your email platform. It never sends email itself. Every kept fact carries the URL it came from and the exact quote that proves it. Anything it can't prove is dropped with a reason.

One scheduled job runs the whole path, and every lead has its own state machine with a repair cycle. A step that fails is retried with backoff, a run that dies mid-lead resumes from the last checkpoint, and a lead that keeps failing is retired with the reason instead of sitting half-researched. A backlog command re-checks the stranded pile and recovers what can still be proven.

Project page: [daymionalvarez.com/portfolio/lead-engine](https://daymionalvarez.com/portfolio/lead-engine)

## How it works

Each run reads the queue from SQLite, searches Google Places for more businesses when the queue runs low and paid discovery is enabled, and pushes due leads through four steps. **Verify** reads the homepage and its about, team and contact pages, and looks for a named owner, founder or similar, an address published for that person, and facts for the opener. **Write** fills a template from one quoted fact, or asks a Claude model for a line built only from the cited facts. **Gate** checks the suppression list and duplicates, proves every fact again against the stored page, and applies the voice rules again. **Hand-off** adds the lead to an Instantly campaign, as a dry run by default. Every finished step is checkpointed before the next one starts.

## Quickstart

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/alvarezdaymion/lead-engine.git
cd lead-engine
uv run lead-engine demo        # fictional businesses from local HTML; no keys, no network
```

To use it on real businesses, run `uv run lead-engine init`, edit `lead-engine.toml` and `voice.toml`, copy `.env.example` to `.env` and fill in the keys, then run `uv run --env-file .env lead-engine check` and `uv run --env-file .env lead-engine run`. Use `--env-file .env` for manual commands that need credentials; the CLI does not load the file itself. Or hand the repo to your coding agent and say "set up lead-engine for my business": [SETUP.md](SETUP.md) is written for it.

Paid discovery starts disabled, even with a key present. To authorize billed Places searches, set `[discovery] allow_paid = true`; `lead-engine check` reports whether it is enabled. CSV import works without this opt-in. Hand-off dry-run mode does not control discovery billing.

The demo requires an empty destination and marks it with `.lead-engine-demo`. To rerun it, use `uv run lead-engine demo --reset` (and the same `--dir` if you chose one). Reset is refused for unmarked directories.

## What you see

The demo runs three scheduled runs on a simulated clock, then a backlog pass and an export. All businesses, people and addresses in it are fictional.

1. **Run at 09:00.** Four searches (two markets by two keywords) find 10 places. Three share one website and are skipped as a chain. Harbor Ledger Co. becomes campaign-ready. Brightline Books' about page times out, so the lead is retried in 30 minutes instead of dropped. Summit Tax Partners names nobody and is dropped with `decision_maker_not_found`. Oakridge Bookkeeping's site answers HTTP 503. Then the run itself is killed while writing Copperfield Inspections' opener (simulated), and its lease stays held.
2. **Run at 09:31.** The expired lease counts as one failed attempt. Copperfield resumes at `verified` without fetching its site again and becomes ready. Juniper Home Inspection only publishes a shared inbox (`no_direct_email`). Stonegate's owner is on the suppression list (`suppressed`). Brightline and Oakridge fail a second time and back off 60 minutes.
3. **Run at 10:32.** Brightline's about page loads on the third try and the lead is repaired. Oakridge fails a third time and is retired with `repairs_exhausted` and the last error.
4. **Backlog.** Three half-researched leads from another tool and the retired one are verified again from fresh reads. Bob Calder is proven as Robert Calder (same person, recovered). Northwind Lawn Care now names a different owner (changed; the lead continues with the new person). Fernhill Plumbing proves nothing (dropped). Oakridge is still down (inconclusive, left as it was).
5. **Export.** Five campaign-ready leads with their openers and evidence go to `out/ready.csv`, and the hand-off dry run writes the exact Instantly requests to `out/handoff/`. Nothing is sent.

## How it runs

`lead-engine run` is one run. Schedule it every 15 minutes (launchd, cron and systemd examples are in [examples/scheduling](examples/scheduling)).

1. A file lock makes a second, overlapping run exit at once (macOS and Linux; elsewhere, don't let the scheduler overlap runs).
2. Leases that expired without a release mean a worker stopped mid-step. Each counts as one failed attempt, and the lead resumes from its last checkpoint.
3. With `discovery.allow_paid = true`, when fewer than `min_queue` leads are due, the next Places searches run: (market, keyword) cells never searched first, then the oldest once `refresh_days` have passed. Results without a website, directory and social profiles, known or suppressed domains, and websites shared by more than `max_locations_per_domain` places (chains) are skipped.
4. Due leads are claimed one at a time, most advanced first, and pushed through verify, write and gate. Each finished step is checkpointed in SQLite.
5. A failed step keeps the lead at its last checkpoint and schedules a retry after 30 minutes, then 60, 120 and so on up to `max_backoff_minutes`. After `max_repairs` failed attempts at one stage (3 by default) the lead is retired with the stage and the error.
6. When the model API fails (rate limit, outage, bad key) the lead is released untouched and the run stops; that never uses a repair attempt. Five failures in a row also end the run, because that pattern usually means a shared problem such as a network outage.
7. Ready leads are handed to the email platform (dry run by default). The run's summary is stored and printed.

## The repair cycle

```
discovered --verify--> verified --write--> written --gate--> ready --hand-off--> handed_off
     |                    |                   |
     +--------------------+-------------------+--> dropped (reason): proven unfit or unprovable

a failed step: same stage, attempt + 1, retry after backoff --> retired (reason) after max_repairs
```

- **Checkpoint every step.** A lead's stage, evidence and opener are written to SQLite when each step finishes. A run that dies loses only the step in flight.
- **Lease, then resume.** A worker leases one lead for `lease_minutes`. A lease that runs out was abandoned: the next run counts it as a failed attempt and resumes the lead. A lead that keeps killing its worker is retired like any other repeated failure, so it can't stall the queue.
- **Retry with backoff, then retire with a reason.** No lead stays half-researched. It moves forward, is dropped with the reason it can't be proven, or is retired with the error that kept it from moving.
- **Unreadable is not unproven.** A page that timed out is not evidence that nothing is there. When a page verification wanted could not be read and nothing was proven without it, the step is retried instead of dropped.
- **Outages don't burn budgets.** Model API failures release the lead and stop the run or backlog pass. An uncertain hand-off leaves its leads `ready` for manual reconciliation; a durable write record blocks automatic resubmission.
- **Re-check the stranded pile.** `lead-engine backlog` takes retired leads, research leads untouched for `stale_hours`, and half-researched leads imported from another tool (`--import stranded.csv`, template in [examples/stranded.csv](examples/stranded.csv)). Each is verified again from a fresh read: **recovered** (the same person is proven again; nicknames and suffixes count, so does a matching address), **changed** (a different decision-maker is proven; the old name is recorded and the lead continues), **dropped** (nothing can be proven any more) or **inconclusive** (the site can't be read right now; the lead is left exactly as it was). Recovered and changed leads get a fresh repair budget and continue to write and gate in the same pass.

`tests/test_engine.py` and `tests/test_backlog.py` cover each rule: stage transitions, a step that fails twice then succeeds, a lead retired after max repairs, the backoff cap, resume after a crash mid-run without fetching again, a lead that keeps killing its worker, shared outages, and every backlog outcome.

## Evidence rules

Extraction uses a fixed allowlist, and the gate replays the same functions on all stored snapshots. There is no role inference or partial-phrase extraction. Text outside these forms cannot supply evidence.

Inline text is joined within each block before matching sentences and addresses. Direct text in a parent element is kept as its own block; child blocks separate adjacent runs, so separate cards are never joined. Whitespace is collapsed and en/em dashes are folded to `-`. A name is exactly two or three capitalized ASCII words (`Pat Example`, `Pat Alex Example`); initials, particles and apostrophes are outside this grammar. Role and business comparisons ignore case. A business must match the lead's company exactly after whitespace and case normalization. A company may have the same name as its owner. No extra clause, number, question, alternative or qualifier may be appended. A single final period is optional for identity and text-contact forms.

**Decision-maker text** must be one whole sentence or one innermost block element matching one of these six forms:

| Accepted form | Example |
|---|---|
| `<Name>, <Role>` | `Pat Example, Owner` |
| `<Name> - <Role>` | `Pat Example - Owner` |
| `<Role>: <Name>` | `Owner: Pat Example` |
| `<Name> is the <role> of <Business>` | `Pat Example is the owner of Shop` |
| `<Name> is our <role>` | `Pat Example is our owner` |
| `Meet <Name>, our <role>` | `Meet Pat Example, our owner` |

`<Role>` must be in `[verify] roles`. Separate name/title cards are not joined. Independent sentences are checked separately: `Pat Example, Owner. Lee Sample retired.` still proves Pat's role. If an unsupported sentence repeats a proven full name, that person's eligibility is unproven, including descriptions of retirement or of having an assistant. Those descriptions are not interpreted or repaired. Each merged nickname variant must have its own complete proof; the strongest configured role wins.

**Schema** accepts only a Person object with exactly `@type`, `name`, `jobTitle` and `worksFor`: `{"@type":"Person","name":"Pat Example","jobTitle":"Owner","worksFor":{"name":"Shop"}}`. `worksFor` contains exactly the prospect's `name`. The same name grammar, collapsed name whitespace and configured role list apply; the original schema quote is preserved. Such a Person may be nested inside an organization, but founder/owner/employee properties alone never imply a role or affiliation.

**Addresses** must match one complete contact element:

- `<Name>: <email>`
- `Email <Name>: <email>`
- `<Name> <email>`
- A mailto link whose complete enclosing element text is exactly `<Name>` or `Email <Name>`, without a final period.

The full name must be an individually proven decision-maker spelling. First names and address patterns never bind a contact. The displayed `Email` prefix and names use the spelling shown above. A paragraph or other innermost block is one element; inline formatting and `<br>` remain part of its text. Extra text before or after a line break rejects the contact. Every occurrence of an address is retained and checked, including addresses split across inline tags; one invalid occurrence rejects that address. The address must also be on the business's domain, or an allowed free-mail domain, and cannot be a shared inbox. Research, snapshot replay and the gate use the same hostname normalization: lowercase, without a leading `www.` or port.

**Opener facts** require one complete sentence, including its final period. Subject and fixed-phrase comparisons ignore case. `<Subject>` is only `We`, `Our firm`, `Our shop`, `Our company`, `Our team` or the prospect's exact company name.

| Accepted form | Stored fact |
|---|---|
| `<Subject> was founded in <YYYY>.` | `founded`: year |
| `<Subject> opened in <YYYY>.` | `founded`: opening year |
| `<Subject> has served <Place> since <YYYY>.` | `serving`: place and year; never a founding year |
| `<Subject> specializes in <Specialty>.` | `specialty` |
| `We specialize in <Specialty>.` | `specialty` |
| `<Subject> won <Award> in <YYYY>.` | `award`: award and year |

Years are four digits from 1900 through the current year. `<Place>` must equal the city/area portion of the lead's market before its first comma. The specialty slot accepts only: `bookkeeping`, `payroll`, `tax preparation`, `financial reporting`, `bookkeeping for restaurants and cafes`, `older homes and historic districts`, `weekly mowing and spring cleanups`. The award slot accepts only `Best Bookkeeper`, `Best Shop`, `Best Home Inspector`. These finite slots cannot consume other names, ranges, alternatives or hedges. Years-of-experience claims, subjectless fragments and other phrasings do not qualify.

Section headings must be exactly the prospect's name, a proven full person name for identity/contact evidence, or one of: `About`, `About us`, `Our team`, `Meet the team`, `Team`, `Contact`, `Contact us`, `Our story`, `History`, `Leadership`, `Services`. Each enclosing heading must qualify. Quotes inside `blockquote` or `q` never supply evidence.

Every fact keeps its source URL, method and complete quote. Every fetched snapshot is retained, including leaf and direct parent text boundaries, section headings, quotations and repeated contacts. Verification checks serialized snapshots before checkpointing. The gate and both hand-off modes require evidence version 3 and a complete, valid snapshot manifest; legacy ready leads are dropped with `incomplete_evidence` before export or POST. Re-qualify stranded leads with fresh backlog reads. Unsupported identities, contacts and facts drop with a reason instead of consuming repair attempts; unreadable pages retry. `lead-engine export` writes retained facts to CSV.

Existing `voice.toml` files need the `serving` templates and the updated award placeholders (`{award}`, `{year}`) from the example voice file. Years-of-experience facts are no longer extracted.

## Voice rules

`voice.toml` holds the rules every opener must pass: maximum length, maximum sentences, maximum reading grade (Flesch-Kincaid), banned phrases, no greeting (the email template has one), no em dash, no first name, no links, and an opening that differs from the last few openers. The evidence rule is checked too. An opener must cite at least one fact, and every number and capitalized name in it must appear in a cited quote, the company name or the market. That catches invented specifics such as a wrong year or a town the site never mentions. It can't judge every claim, so templates may only restate their fact and the model writer is told to use the cited facts only.

Numbers must be complete numeric values. Mixed letter-and-digit forms such as `20th` and `20k` require that exact token in the cited evidence, company name or market; matching part of a year or converting an unsupported suffix is insufficient.

Two writers:

- `template` (default, no keys) fills the `[templates]` in voice.toml with one fact's values. The order rotates per lead so consecutive leads open differently. Each default kind has six distinct openings for the five-opener exclusion window. Custom templates need more distinct openings than `distinct_window` that pass every voice rule with the expected fact values; check reading grade as well as distinct prefixes. The default specialty openings cover repeated tax preparation, financial reporting and bookkeeping facts.
- `claude` (optional): `uv sync --extra llm`, set `ANTHROPIC_API_KEY` and `[writer] mode = "claude"`. The model receives the facts, the voice rules and the recent openings, and returns strict JSON with the line and the ids of the facts it used. Addresses are redacted from every prompt field; original evidence stays local. A draft that fails the rules gets one repair round with the problems listed; a second failure is a failed attempt in the repair cycle.

## Connectors

| Connector | Status | What it does |
|---|---|---|
| Google Places API (New), Text Search | Real connector | Businesses for a keyword in a market, with website, address and status |
| CSV import | Real | Businesses you already have ([examples/import.csv](examples/import.csv)) |
| Website fetcher | Real | robots.txt, timeouts, size cap, per-host spacing. HTML only, no JavaScript rendering |
| Instantly API v2, add leads | Real connector, dry run by default | `POST /api/v2/leads/add` to one campaign |
| Instantly API v2, accounts | Real connector, read-only | Warmup settings for the audit |
| Instantly API v2 test email, plus IMAP | Real, manual commands | The inbox-placement seed test |
| Anthropic API | Optional extra | The model writer |
| Demo | Synthetic | Fictional businesses on example.com and example.org, served from local HTML |

There is no email-finder, email-verifier or CRM connector. Sites that need JavaScript to show their text are read as served, which usually means no decision-maker is found and the lead is dropped.

## Inbox placement and warmup

A high warmup score does not prove that emails reach the inbox. Two commands check deliverability; neither changes your campaign.

- `lead-engine warmup` reads every sending inbox's settings and flags values outside the ranges in `[warmup]`: account and warmup status, daily limit, sending gap, warmup volume, reply and other warmup rates, warmup score, and ramp-up turned off on a new inbox. A rate between 0 and 1 gets a hint, because the platform reads those fields as percentages. `--strict` exits 1 when an inbox is flagged.
- `lead-engine placement send` sends one test email from every sending inbox to the seed mailboxes you own (`[[placement.seeds]]`), with a run token in the subject. It is a dry run unless you add `--send`. `placement read` opens each seed over IMAP, read-only, and finds each message in the inbox or spam folder by the token and the From address. `placement report` prints each sending domain with **keep** only if every seed message from every inbox on it reached the inbox; any spam or missing message benches the domain. Moving inboxes in or out of a campaign stays your decision. Gmail's Promotions tab is not visible over IMAP, so "inbox" means the INBOX folder.

Seed addresses are stripped of surrounding whitespace and lowercased consistently for configuration, sending, IMAP lookup and placement result keys.

Seed runs are saved before the first request and after each accepted send. If interrupted, resume with `uv run --env-file .env lead-engine placement send --send --run out/placement/run-seed-TOKEN.json`, using the actual saved filename. Resume requires unchanged senders, seed addresses, subject and body. It sends only pending entries, skipping accepted and unresolved entries. A new run is blocked while an earlier run is unfinished. Inspect unresolved sends in the platform and seed mailboxes; never turn them back into pending entries without proving that no send occurred. Any spam copy benches the domain even if an inbox copy also exists.

Reading and resuming a run require its file to be in the configured placement output directory, with a filename matching the run token. Both commands hold that directory's lock throughout loading and saving. An overlapping command exits with an error; a different config cannot bypass the lock to rewrite another directory's run.

## Safety rules

- **This tool never sends email to prospects.** It adds leads to a campaign, and the platform sends on the campaign's own schedule and settings. The Instantly client has exactly two write paths, adding leads and one test email for the seed test, and refuses any other path before a request is made. It cannot launch, pause or edit a campaign or an account.
- **Hand-off is a dry run by default.** The dry run writes the exact requests live mode would send to `out/handoff/` and leaves leads `ready`. Live mode needs `INSTANTLY_API_KEY`, `INSTANTLY_CAMPAIGN_ID` and `[handoff] mode = "live"`. `skip_if_in_workspace` is always set. SQLite records intent before each POST; malformed replies, timeouts and crashes leave that intent unresolved and block resubmission. Only a fully accounted batch can mark leads handed off or skipped.
- **Suppressions are checked three times:** domains at discovery, addresses and domains at the gate, and a fresh read of the configured CSV right before each live hand-off batch. Missing files, invalid headers and malformed entries stop processing; an empty list requires a valid header-only CSV.
- **The seed test only sends with `--send`,** and only to the seed mailboxes you list.
- **Fetching is polite.** robots.txt is obeyed, the user agent names the tool (put your own contact URL in `[fetch] user_agent`), requests to one site are spaced out, and only pages on the business's own site are read. Every website redirect is checked before following it. API redirects are rejected.
- **Secrets** come from environment variables. Model failures report only their exception type and HTTP status, excluding provider response text. Every model client construction failure, including certificate or configuration errors outside SDK exception classes, releases the lead and stops the run without using a repair attempt. Website credentials, query strings and fragments are removed before persistence, export and hand-off; opening existing state also cleans its stored website URLs.
- **Your data stays local.** State, exports, `lead-engine.toml`, `voice.toml` and CSV files at the repo root are git-ignored.
- You are responsible for following the laws and platform rules that apply to the emails your campaigns send.

`lead-engine status` reports hand-offs awaiting reconciliation. Inspect the `handoff_writes` table in `state/leads.db` and the platform's records before any manual correction. Keep unresolved records until the outcome is proven; restarting a run does not clear them.

## Configuration

Everything lives in `lead-engine.toml` (start from `uv run lead-engine init`; defaults are in `src/lead_engine/config.py`):

- `[discovery]`: Places or CSV, `allow_paid` (false by default; explicit permission for billed Places searches), markets, keywords, queue size, searches per run, chain and refresh rules.
- `[verify]`: decision-maker roles, pages per site, required and excluded terms, free-mail rule.
- `[fetch]`: user agent, timeout, size cap, spacing between requests to one site.
- `[engine]`: max repairs, backoff, lease, time and lead budget per run, failures in a row that end a run, when a lead counts as stale.
- `[writer]`: template or claude, the voice file, model settings.
- `[gate]`: the suppression file. `[handoff]`: off, dry-run or live, and batch sizes.
- `[warmup]` and `[placement]`: audit ranges, seed mailboxes and the test email.

Credentials come from environment variables, never from this file (see [.env.example](.env.example)).

## Cost

Verification fetches websites directly and costs nothing. Paid discovery is disabled until `discovery.allow_paid = true`, regardless of hand-off mode. Each Places Text Search request that asks for websites is billed at Google's Text Search Enterprise rate; `cells_per_tick` and `pages_per_search` bound the requests per run. The template writer is free. The model writer makes one call per draft and at most two per attempt. Instantly calls are covered by your plan.

## Repo layout

```
src/lead_engine/
  cli.py          commands
  config.py       lead-engine.toml schema and defaults (templates/lead-engine.example.toml mirrors it)
  engine.py       one run: reap leases, discover, claim, step, repair cycle, hand off
  store.py        SQLite state: stages, leases, attempts, backoff, events, runs
  verify.py       decision-maker, address and opener facts from the business's own site
  fetch.py        the polite fetcher
  html.py         HTML to text, links, addresses and JSON-LD
  evidence.py     page snapshots and fact checks
  names.py        name splitting and same-person matching
  voice.py        voice rules and the evidence check for openers
  writer.py       template and Claude writers
  gate.py         suppressions, duplicates, proof again before ready
  backlog.py      stranded-lead re-qualification
  handoff.py      dry-run and live hand-off
  instantly.py    Instantly API v2 client with the write allowlist
  placement.py    inbox-placement seed test
  warmup.py       warmup settings audit
  report.py       terminal summaries and the CSV export
  sources/        Google Places, CSV import, discovery filters
  demo.py         the synthetic demo; demo_data/ holds its fictional sites
  templates/      example config and voice rules
examples/         CSV templates and scheduling (launchd, cron, systemd)
tests/            pytest; fake transports only
```

## Development

```bash
uv run pytest
uv run ruff check
```

Runtime code uses only the Python standard library. The `llm` extra adds the Anthropic SDK for the model writer.

## License

MIT. See [LICENSE](LICENSE).
