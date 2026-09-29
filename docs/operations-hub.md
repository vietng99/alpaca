# Engineering operations hub

The home page `/` is the workspace hub: one tile per operations workspace, each with a live summary (open operation and its progress, task counts, live sessions, next action) read from that workspace's `/board/data.json`. Today the registry (`serve.workspaces`, served at `/workspaces.json`) holds this project only; the Alpaca tile opens Cockpit at `/hub/`. `/hub/` and `/cockpit/` open Cockpit in the shared operations workspace, and its sidebar links back to the hub. `/analytics/` opens Sessions in the same navigation; existing `/analytics/#lib` bookmarks open Library. The previous cockpit remains at `/cockpit/legacy/` and the previous analytics page at `/analytics/legacy/`. The classic board is retired: `/board/` and `/board.html` redirect to `/hub/`, while `/board/data.json` still serves the record payload the cockpit reads.

## Signing in

With a login configured (`.alpaca/files-auth` or `ALPACA_FILES_AUTH`, form `user:code`; an empty user means code only), any locked page answers 401 with the sign-in page in place, so the address stays the same and the browser shows no native password dialog. The page posts the code to `/auth/login`; a correct code sets `alpaca_session_<8 hex of the instance id>` (one cookie per instance, so two instances never share a sign-in slot; a cookie under the old name `alpaca_session` reads as signed out), an HttpOnly, SameSite=Lax cookie (Secure over the tunnel) that lasts 12 hours. The cookie is an expiry plus an HMAC-SHA256 over the expiry and a digest of the credential, keyed by `.alpaca/web-session-key` (created 0600 on first use). Changing the credential signs every browser out; deleting the key file does too. Sign out on the hub posts `/auth/logout`. HTTP Basic credentials remain accepted for scripts and curl. Failed attempts on either path share one throttle (`alpaca/weblogin.py`): 8 failures from one client in 15 minutes lock that client, and 60 failures from all clients lock sign-in for everyone for the rest of the window. Behind Cloudflare the client is the `CF-Connecting-IP` address. Data routes answer a JSON 401 with `"login": "/login"`.

The sign-in page shows a rotating world globe (coastlines only: no place markers, routes or highlighted country), a relay feed of link telemetry, binary rings around the code field and an unlock sequence; the hub shows one tile per workspace in the same style. They support light and dark themes and reduced motion. Both pages open in the dark theme; the theme button stores the choice in the browser under `alpaca.login-theme` (sign-in page) or `alpaca.hub-theme` (hub), separate from the cockpit's `alpaca.theme`. Their files are `alpaca/web/login.html`, `workspaces.html`, `vault.css`, `vault.js` and `vault-globe.js`, served publicly from `/login/assets/` together with the coastline data `countries-110m.json` and the fonts under `vendor/` (origins and licenses: `THIRD-PARTY-NOTICES.md`).

## Reading the work

- Cockpit is the live engineering view: current assignment, operation completion ring, the current stage map when the project's profile supplies stages, active runs, recent completions, next action, and meaningful activity. Unbound instrument diagnostics stay in Activity (Work and checks / All recorded events), while the cockpit shows task progress and any bound profile results and capture failures. It refreshes every 15 seconds when visible; Pause and Focus view support a stable display. Refresh defers while a control in the main view or navigation has focus. Assignments are not evidence of a running process. Task percentages show counts, not estimated time remaining. A pinned operation (`bin/alpaca op pin OP`) adds an Operation map above the roadmap: the op's phases in order with a phase strip, each phase's tasks by workstream, prerequisite arrows, owner holds and blocked tasks; finished tasks stay on the map as done.
- Overview leads with the current assignment, work progress, current acceptance, recent changes, and reports. Machine resources do not stand in for work progress.
- Work & evidence separates tasks from acceptance outcomes. Use numbered cards or a table. Each completed task shows the completing event time and session, delivered work, result, expandable method, and proof. The full record includes purpose/source and lifecycle history. Assigned workers and sessions are distinct; missing provenance stays explicitly unrecorded. Reopened tasks retain historical reports with a warning. Open an acceptance outcome for its current check, recorded completion, verification method, source, and proof. Internal specification checks are not counted as completed engineering outcomes.
- Every open task card says whether the task has a contract. The task detail shows it as four cards: input, expected output, done bar and fail cases, with who recorded it and the source it restates. Record or replace one with `bin/alpaca task contract <id> --input ... --expected ... --done-bar ... --fail-case ... [--source ...]` (each flag repeatable, or `--file <json>`); `task add` takes the same flags. Each call appends a `task-contract` event and the newest is current. A contract restates the task; the task still closes only with a sealed proof report.
- Runs & logs and Live logs show the runs a domain profile records: historical attempts with their verdicts, a selected run's log with flagged lines, and one live tile per running job. With no profile these views have no runs to show. Historical PASS does not certify current inputs.
- Activity pages through the matching record with a stable cursor. Work and checks is the default filter; Task and flow progress omits unbound instrument diagnostics; All recorded events includes routine capture and lifecycle traffic. Original event details remain available.
- Messages collects the recorded reports and handoffs, with recipient and type filters and links to the task and session.
- Library starts with project reports, proofs, and decisions. Filter Markdown or HTML independently. Tool documentation is a separate collection. HTML previews are sandboxed without scripts, network requests, or parent-page access.
- Sessions explains token usage, estimated API cost, context growth, actions, hooks, and the visible conversation. Select a session, then use Overview, Costs & tokens, Context window, Actions & hooks, Agent crew, Conversation & tools, or Work & reports. Capture coverage is explicit. Tokens are measured from source counters, never inferred from activity counts.

Light, Dark, and System themes are available in the top bar on every shared page, including analytics, logs, and detail readers. The preference persists locally and synchronizes across tabs; System follows the device setting. Sandboxed HTML reports retain their authored appearance.

Task status distributions, dated completions, run outcomes, activity counts, message types, and library formats support quick reading. Counts identify their scope and loaded subset; charts keep exact labels and links to the underlying records. Missing values never become a zero or a guessed completion.

Navigation stays available at desktop and mobile sizes. Ctrl+K or Cmd+K opens workspace search. Browser back/forward restores views and filters; Escape closes a detail reader.

## Maintaining a useful checklist

The cockpit's Task roadmap and Work & evidence's grouped checklist read the same plan. Work areas such as CI/CD, Visualization, Features and Documentation contain named workstreams such as Release pipeline or Cost analytics. Phases still describe delivery stages; they are separate from work areas. Arrows record actual prerequisites. Task numbers and screen positions never create dependencies. The roadmap uses compact task cards inside workstream sectors that share rows when space permits. Open a card's actions button for details, chat and dependency tracing. The Task roadmap header collapses the entire map and remembers its state through live refresh; individual workstreams can also fold while retaining external arrows.

Start with all operations and Active work. Choose a work area or search the operation picker to narrow the view; Full plan includes completed nodes. Completed workstreams start collapsed. Chat choices use the assigned work's name. Trace highlights immediate prerequisites and successors. Compact task cards wrap within the available width, including on phones; card position does not define order. Continuous arrows show recorded dependencies and connect to workstream headers when groups are collapsed. Details expands a card to show prerequisites and reasons. Group expansion, task details, filters and focus survive refresh.

Agents organize tasks as they create them:

```sh
bin/alpaca --session SESSION_ID task group op-001 release --title "Release pipeline" --category "CI/CD"
bin/alpaca --session SESSION_ID task add op-001 "Build the verified release" --title "Build release" --group release --independent --resource release-source
# For a follow-up, inherit only the existing task's group and name the actual prerequisite:
bin/alpaca --session SESSION_ID task add op-001 "Check the release" --title "Check release" --inherit t-001 --after t-001 --resource release-source
bin/alpaca --session SESSION_ID task map t-002 --group release
```

Replace example IDs with the recorded operation and task IDs. Create or reuse a group before adding a task. `task add` saves the task and its mapping together; a rejected mapping saves neither. Supply `--group` or `--inherit`, `--after` (repeatable) or `--independent`, and `--resource` (repeatable) or `--no-resources`. `--requires-stage` is repeatable. Inheritance copies only group membership, so prerequisites and resources stay explicit. `task map` preserves any omitted fields and all other tasks; a first mapping needs the same three declarations as creation. Changing a group never clears an existing hold.

`task planning --require` enables a project-local policy rejecting new unmapped CLI tasks. `--show` reads it; `--allow-unmapped` restores compatibility. Legacy and internal intake tasks remain readable as Needs organization until mapped. Do not guess their constraints to make them appear ready. Add dependencies only when one task consumes another's output; use resources for shared-file or machine contention. Review groups and prerequisites when splitting work or changing direction.

Read or author a plan through the normal task API:

```sh
bin/alpaca task plan op-001 --show
bin/alpaca task plan op-001 --file plan.json
```

A plan file includes the revision returned by `--show` (0 before the first plan), the ordered groups, and explicit constraints for each planned task:

```json
{
  "version": 1,
  "revision": 0,
  "title": "Release pipeline",
  "summary": "Move verified work into a public release.",
  "groups": [{"id": "implementation", "title": "Release pipeline", "category": "CI/CD"}],
  "tasks": [
    {"id": "t-001", "group": "implementation", "after": [], "resources": [], "requires_stages": [], "hold": null},
    {"id": "t-002", "group": "implementation", "after": ["t-001"], "resources": ["build-machine"], "requires_stages": [], "hold": null}
  ]
}
```

All task references must belong to the operation and appear in the plan. A task with multiple prerequisites waits for all of them. Plans reject cycles, duplicate or missing references, unknown fields and updates based on an old revision. Read the latest plan and reapply your changes after a revision conflict. Up to 500 tasks and 80 groups can be planned per operation; tasks omitted from the plan remain Needs organization. Optional `title` (80 characters), `summary` (240) and group `category` (60) supply display labels; old snapshots remain valid. Use explicit empty arrays to record that no known task, stage or resource constraint applies.

`hold` is null or an object such as `{"kind":"approval","reason":"Owner chooses the target"}`. Supported kinds are `approval`, `resources` and `blocked`. Replacing the plan changes or removes the hold; it does not change task status or grant execution permission. `requires_stages` names profile stages that need current PASS evidence. The existing phase and execution gates still apply.

Ready means recorded prerequisites have valid completion reports, required stage evidence passes, the task's phase has been opened, and no recorded hold or active resource conflict is found. A valid historical report does not establish current domain input validity; record the required profile stages for that check. Named resources are exclusive: ready tasks sharing a resource are alternatives, and a task recorded in progress retains its resource constraint even when its chat assignment expires. Active tasks with unplanned constraints make resource availability unknown. Assignment and progress labels do not certify that a process is running.

Generated `CHECKLIST.md` includes the same groups and prerequisite labels. Its roadmap checks completion seals and current stage evidence at render time without running a stage. These observations can age; the live view refreshes them.

`CHECKLIST.md` is generated from the shared record. Its first section numbers each task and records purpose, source operation, creation/update/completion time, actual completing session, method, result, next action, and work report. Report excerpts are bounded; their historical seal is not a fresh proof check. Its acceptance section groups acceptance outcomes and labels them as the last recorded check. Detailed internal obligation rows remain below those sections for audit and agent tooling. The live hub separately checks current evidence validity.

Add justified work through the normal task API after creating its group, for example:

```sh
bin/alpaca --session SESSION_ID task add op-006 "Explain the failed integration test and its recovery" --title "Explain failed test" --group verification --independent --no-resources --phase verify --why "Owner request: engineers need the failed test, the cause, and a reproducible recovery"
```

The title is the short name the pages show; the statement is the full description. Short names for
acceptance items may be given in an optional `docs/checklist-titles.json`, keyed by item id, so the spec
tables stay unchanged; without it the pages show the full statement.

Write the report as engineering documentation: what changed, why, how it was done, result, deviations, how to reproduce, evidence, and next action. Use `proof new`, `proof seal`, and `task move ... done --proof local:REPORT` to record completion. Keep agent updates and human-directed reports in the message record, not only the terminal stream.

## Capture and limits

Codex lifecycle is explicit. Start or resume the same project session with its registered transcript path:

```sh
bin/alpaca --session SESSION_ID session start --operator codex --transcript /path/to/this-session.jsonl
```

The summary collector now recognizes Codex visible records as well as the existing Claude format. Conversation detail preserves repeated human prompts, pairs calls/results, ignores private analysis records, and labels record-only fallback. Only explicitly registered or project-local sources are read. It never searches another account's transcript store.

Conversation reads have a 32 MiB total source budget, 2 MiB JSONL record limit, 256 KiB content limit, and at most 40 child sources. Partial reads, missing sources, and lower-bound totals are identified in coverage. The report reader shows up to 512 KiB of text. Logs load in 64 KiB blocks and keep the latest 4 MiB in browser memory; earlier output can be reread from the beginning. Save loaded output exports only the loaded portion.

## Understanding session analytics

The response is the unit of usage. Claude can emit several content blocks with the same response ID; those blocks count once. Codex native `token_usage_record` counters take precedence over repeated `token_count` snapshots. Snapshot-only sources have separate reset and missing-prefix checks. Reasoning token counts remain part of output; private reasoning text is never displayed.

Input includes fresh input, cache reads and cache writes exactly once. Tokens processed means the sum across requests, so rereading the same cached context increases that total. Context history instead shows the input size of each request. A capacity percentage appears only when the source records the capacity. Compactions are explicit records, not guesses from dips in the chart.

Costs have three separate sources:

- Estimated API cost multiplies measured response usage by an exact model rate card in `alpaca/analytics/pricing.py`. Each priced response carries the official source URL, verification date, category prices, service assumption and applicable context multiplier. These are current list-price estimates, not historical invoices or subscription charges.
- Client cost snapshot preserves the client's `cost-state` amount and provenance. Its scope can include child agents and other activity, so it is not added to the main conversation estimate.
- Imported reported cost comes from explicit project `usage-import` records. It remains separate from both calculations above and shows incomplete imports as a subtotal.

Costs & tokens contains a response ledger with text/model/action filters, sorting, CSV export, and an inspector. Open a response in the conversation to see the surrounding visible text and tool results. Earlier and later controls continue through the captured conversation. Context-chart points also link to their source neighborhood. The JSON download includes the complete bounded analytics result, its measurements, rate cards and coverage.

Actions & hooks separates outer tool error flags from nested command/action failures. Hook executions, context injections and stop summaries retain their event names and types. Tool cost allocation divides a response estimate equally across its calls; it is not the intrinsic cost of a tool. The per-call average uses priced calls only.

The default session scope is one conversation. Agent crew analyzes the main transcript and each discovered child separately before adding their estimates. Client snapshots are never added to that combined estimate. The project index shows main conversations only. A tool pool can contain child activity; an available transcript establishes which calls belong to the selected conversation. Pool-only records remain usable when the transcript is unavailable.

Analytics scans use a separate 64 MiB source budget and 16 MiB record limit so large compaction records do not hide subsequent usage. Tool arguments/results and hidden compaction histories are not retained by the analytics scan. The ledger holds at most 5,000 responses, hooks at most 2,000 records, and source discovery at most 40 children. Any limit is reported; missing usage, source gaps, unreadable files or unknown rates never become a zero cost. The bounded memory cache tracks source revisions, including imported usage events.

The read services are `/hub/analytics-index.json`, `/hub/analytics.json?sid=SESSION`, and `/hub/analytics-children.json?sid=SESSION`. They use the existing web login and registered/project-local sources. No account-wide transcript scan or external analytics service is involved.

Run metadata is read independently per file; malformed metadata is reported without hiding valid runs. A document's recorded seal is historical; full proof verification still uses `alpaca proof check`.

### Rate cards, pricing gaps and the cost selftest

The estimated API cost of a response comes from a rate card: the provider's published
list price per million tokens for each token category. `alpaca/analytics/pricing.py` holds
the cards verified when the code was written. A model released later has no card there, so
its responses show "Unavailable" until someone records one. That record is an agent's job
and needs no code change:

1. **Detect.** The Sessions page lists every model that answered without a card, with the
   command that fixes it. The cost selftest fails on the same gap, and the dashboard and the
   selftest leave `.alpaca/analytics/pricing-gaps.json`; the next session start prints a
   `PRICING GAP` line from it.
2. **Check and label.** For a Claude model, fetch the official price page and record its row:

```bash
bin/alpaca analytics price-check --model claude-example-9-1 --dry-run
bin/alpaca analytics price-check --model claude-example-9-1
bin/alpaca analytics price-check        # every current gap
```

   `price-check` downloads `https://platform.claude.com/docs/en/about-claude/pricing.md`,
   keeps one copy per content hash under `.alpaca/analytics/pricing-sources/`, reads the
   model's row of the "Model pricing" table and the "Fast mode pricing" table, and appends
   one `ratecard-verified` event with the rates, the source URL, the page sha256, the row
   text and the date. It refuses when the table header differs from the five expected
   columns, when no row or more than one row matches, or when a price cell does not parse.
   The page and every redirect must stay on https `platform.claude.com`; the final URL is
   the one recorded. It refuses a model that already has a built-in card; an identical
   repeat (same rates, source and page hash, on any day) records nothing. Rates must lie
   between 0 and 10000 USD per million tokens (input and output above 0) and a fast mode
   multiplier between 1 and 10. For another provider, read its official price page
   yourself and record what it says:

```bash
bin/alpaca analytics price-label --model example-model --provider openai \
  --source https://example.com/official-pricing --input 10 --cache-read 1 --cache-write 12.5 --output 50
```

   Every category the provider bills is required (Claude cards take `--cache-write-5m` and
   `--cache-write-1h`; OpenAI cards take `--cache-write`), and the source must be https.
3. **Confirm.** The live server sees the new event, reprices every cached analysis and
   pushes the change; the Sessions page updates the affected cells in place.

The selftest checks the cost analytics end to end and saves its report under
`.alpaca/analytics/selftest/`:

```bash
bin/alpaca analytics selftest                  # offline checks
bin/alpaca analytics selftest --online         # also compare every Claude card with the official page
bin/alpaca analytics selftest --label-gaps     # record each Claude gap from the official page first
```

Its checks: `arithmetic` (every card against an independent calculation and pinned
known answers), `coverage` (every response of every session with a transcript is priced;
a model without a card fails with its fix command, other unpriced responses warn by
reason), `client-snapshot` (where a transcript carries the client's own cost record, the
client cost must fall between our all-5-minute and all-1-hour cache-write bounds),
`online` and `label-gaps` (skipped unless requested). It exits FAIL when any check fails.
Recorded cards are current list prices checked on the recorded date, not a historical
invoice.

## Operation and rollback

The hub uses the existing Python server, authentication, and Cloudflare origin. No browser CDN or Node build is required. IBM Plex fonts are bundled under their SIL Open Font License; interface icons are original SVG paths.

Run `bin/alpaca serve --detach --keep --port PORT` for the existing project. During development, a separate loopback preview can use `serve.make_handler` without replacing `.alpaca/serve.json`. Before changing an existing deployment, retain the prior source and verify the authenticated public page after restart. The previous views remain available for comparison.


## Mission map

Every workspace has a Mission map page, with an overview on Cockpit. Development is the default view: compact sector and component nodes show linked work progress, while the development plan below shows concrete tasks, workstreams, prerequisites and ready work. Both use the workspace theme and the shared task-roadmap renderer. Select a component and enable Selected component only to focus its tasks. Open tasks without a component link appear under Needs component mapping; link them explicitly instead of guessing from their titles. Components and Change impact views retain search, concern filters, source relationships, checks, dates and recorded changes.

Component colors describe development: in progress, ready, planned, blocked, work complete or no linked work. Completed work requires a currently valid sealed task proof; it does not certify the component's checks. Failed checks and stale evidence remain visible, and verification details distinguish Not checked from Verified. Ready next uses recorded prerequisites, gates and exclusive resources, not the order in which tasks were numbered.

Initialize a map with `bin/alpaca mission init --preset generic`, or use `--preset alpaca` for the harness's ten areas. An unconfigured workspace displays an unknown starter map. `mission show --definition` exports the current definition and revision. Edit the definition object and apply it with `mission define --file FILE --revision REVISION`; a stale revision is rejected. Definitions contain groups, nodes, inventory roots and typed relationships. Capability source patterns are relative to the project. Runtime directories, tool environments and symlinks are excluded. Coverage counts identify the declared inventory, unmapped files and exclusions; the graph does not claim exhaustive dependency discovery.

`mission link NODE TASK --kind work` associates current work. Use `--kind fix` only for an actual repair. A last verified fix requires that explicit relationship, a completed task with a valid sealed proof, and all declared capability checks passing after task completion. Ordinary task completion never marks component verification as passed. Last changed, last verified fix and last checked remain separate.

Execute a declared check with `bin/alpaca mission check --check tests NODE -- COMMAND`. This runs the explicit argument list without a shell and records the command, return code, log and input fingerprint. Verified means all declared checks pass for their current inputs and their retained logs still match. Failures, missing inputs, log changes, incomplete checks and changes during a run stay visible. Choose commands that actually exercise the capability; the generic runner records results but cannot judge whether a command adequately tests a requirement.

Confirmed `feeds`, `depends_on` and `governs` edges point from provider to consumer and propagate input invalidation. Documentation and verification-reference links do not. Inferred edges appear only when Suggested links is enabled and never certify impact coverage. Change impact offers direct or transitive dependents with the relationship reason and explanatory path. Potential impact does not imply failure.

The observability collector records changed source snapshots when it processes project events. `mission scan` captures an explicit snapshot, and map reads also detect current filesystem changes without writing. If no collector event has captured a change yet, its timestamp is labeled a filesystem observation. Historical views use saved source observations, task states and task-plan readiness (older snapshots explicitly report that their development plan is unavailable), with a clear historical label; they do not recompute old health using today's files. Reads and check-log access use the hub's authentication boundary; check-log reading requires a configured login.

The first map includes recorded source history and snapshot selection. It does not infer every code dependency or reconstruct source history that was never captured. Additional relationships and capability requirements are versioned project decisions.
