# Engineering operations hub

The home page `/` is the workspace hub: one tile per operations workspace, each with a live summary (open operation and its progress, task counts, live sessions, next action) read from that workspace's `/board/data.json`. Today the registry (`serve.workspaces`, served at `/workspaces.json`) holds this project only; the Alpaca tile opens Cockpit at `/hub/`. `/hub/` and `/cockpit/` open Cockpit in the shared operations workspace, and its sidebar links back to the hub. `/analytics/` opens Sessions in the same navigation; existing `/analytics/#lib` bookmarks open Library. The previous cockpit remains at `/cockpit/legacy/` and the previous analytics page at `/analytics/legacy/`. The classic board is retired: `/board/` and `/board.html` redirect to `/hub/`, while `/board/data.json` still serves the record payload the cockpit reads.

## Signing in

With a login configured (`.alpaca/files-auth` or `ALPACA_FILES_AUTH`, form `user:code`; an empty user means code only), any locked page answers 401 with the sign-in page in place, so the address stays the same and the browser shows no native password dialog. The page posts the code to `/auth/login`; a correct code sets `alpaca_session_<8 hex of the instance id>` (one cookie per instance, so two instances never share a sign-in slot; a cookie under the old name `alpaca_session` reads as signed out), an HttpOnly, SameSite=Lax cookie (Secure over the tunnel) that lasts 12 hours. The cookie is an expiry plus an HMAC-SHA256 over the expiry and a digest of the credential, keyed by `.alpaca/web-session-key` (created 0600 on first use). Changing the credential signs every browser out; deleting the key file does too. Sign out on the hub posts `/auth/logout`. HTTP Basic credentials remain accepted for scripts and curl. Failed attempts on either path share one throttle (`alpaca/weblogin.py`): 8 failures from one client in 15 minutes lock that client, and 60 failures from all clients lock sign-in for everyone for the rest of the window. Behind Cloudflare the client is the `CF-Connecting-IP` address. Data routes answer a JSON 401 with `"login": "/login"`.

The sign-in page shows a rotating world globe (coastlines only: no place markers, routes or highlighted country), a relay feed of link telemetry, binary rings around the code field and an unlock sequence; the hub shows one tile per workspace in the same style. They support light and dark themes and reduced motion. Both pages open in the dark theme; the theme button stores the choice in the browser under `alpaca.login-theme` (sign-in page) or `alpaca.hub-theme` (hub), separate from the cockpit's `alpaca.theme`. Their files are `alpaca/web/login.html`, `workspaces.html`, `vault.css`, `vault.js` and `vault-globe.js`, served publicly from `/login/assets/` together with the coastline data `countries-110m.json` and the fonts under `vendor/` (origins and licenses: `THIRD-PARTY-NOTICES.md`).

## Reading the work

- Cockpit is the live engineering view: current assignment, operation completion ring, the current stage map when the project's profile supplies stages, active runs, recent completions, next action, and meaningful activity. Unbound instrument diagnostics stay in Activity (Work and checks / All recorded events), while the cockpit shows task progress and any bound profile results and capture failures. It refreshes every 15 seconds when visible; Pause and Focus view support a stable display. Refresh defers while a control in the main view or navigation has focus. Assignments are not evidence of a running process. Task percentages show counts, not estimated time remaining.
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

`CHECKLIST.md` is generated from the shared record. Its first section numbers each task and records purpose, source operation, creation/update/completion time, actual completing session, method, result, next action, and work report. Report excerpts are bounded; their historical seal is not a fresh proof check. Its acceptance section groups acceptance outcomes and labels them as the last recorded check. Detailed internal obligation rows remain below those sections for audit and agent tooling. The live hub separately checks current evidence validity.

Add justified work through the normal task API, for example:

```sh
bin/alpaca --session SESSION_ID task add op-006 "Explain the failed integration test and its recovery" --title "Explain failed test" --phase verify --why "Owner request: engineers need the failed test, the cause, and a reproducible recovery"
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

## Operation and rollback

The hub uses the existing Python server, authentication, and Cloudflare origin. No browser CDN or Node build is required. IBM Plex fonts are bundled under their SIL Open Font License; interface icons are original SVG paths.

Run `bin/alpaca serve --detach --keep --port PORT` for the existing project. During development, a separate loopback preview can use `serve.make_handler` without replacing `.alpaca/serve.json`. Before changing an existing deployment, retain the prior source and verify the authenticated public page after restart. The previous views remain available for comparison.
