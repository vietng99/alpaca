# Alpaca Studio: look and behavior

Status: describes the Studio as this release ships it, including the page registry and the project
slot (section 4.0): a project's own pages, cards and stylesheets through its profile. Section 8 says
what a project may change.

## 1. Purpose and how to read this

The Studio is the web app every Alpaca project serves at `/hub/` (files in `alpaca/web/`). This
document says what a reader sees and what happens when they act, in words you can check in a
browser. Open the Studio next to this text and compare. Where a file or function name helps you find
the code, it is given as extra help; the sentence before it stands on its own. Sizes are in CSS px
and come from the style sheets (`hub.css`, `theme.css` and the page sheets). Times are in
milliseconds (ms) or seconds (s) as the code sets them.

Words used throughout: the **record** is the project's `.alpaca/alpaca.db` as read through the
overview; a **surface** is one of the four things the server watches (data, library, board,
pricing); a **flag** is the browser's note that one cache is stale.

## 2. Layout

### 2.1 The frame

The page is a fixed sidebar on the left and an app column on the right. The app column holds the
topbar, the main area and the footer, in that order.

| Part | Desktop value | Notes |
|---|---|---|
| Sidebar | 232 px wide, full height, fixed, 1 px right border | 210 px between 701 and 1200 px of window width; off-screen at 700 px and below |
| Topbar | 64 px high, 1 px bottom border, sticks to the top while the page scrolls | 56 px on phones |
| Main area | padding 32 px; 24 px up to 1200 px; 16 px on phones; no maximum width | holds one page at a time |
| Footer | one line, 12 px text, 1 px top border | "Alpaca Operations Studio" on the left, "Work. Context. Evidence." on the right |
| Skip link | "Skip to content", hidden until it takes keyboard focus | jumps to the main area |

The browser tab title is "<Page name> | Alpaca Operations Studio". Opening `/hub/` with no address
fragment, or with an unknown one, shows the Cockpit (`#cockpit`). Every page is a fragment of the
address (`#roadmap`, `#sessions?sid=...`), so back, forward and bookmarks work.

### 2.2 Sidebar

From the top:

1. **Brand block.** A 32 px square with a thin border holding the letter "A", then the word
   "Alpaca" (16 px, medium weight, wide letter spacing) with "Operations Studio" in 12 px under it.
   The whole block links to the Cockpit.
2. **Arthur slot and caption.** Arthur the alpaca is drawn 128 px wide (frame ratio 12 to 13) and
   centred, with his caption in 12 px muted text under him. He is a button: a click plays a trick.
   There is no status row under the caption (the old green dot was removed by owner decision).
   Section 6 describes him.
   Between Arthur and his caption sits the **health bar**: "Health N/1000" with a thin fill bar, a
   meter for screen readers. The score comes from `/hub/vitals.json` (section 7.7) and has five
   parts: page data 250, hub speed 250, capture collector 200, capture backlog and issues 150,
   server errors 150. 900 and up is the good band, 600 to 899 attention, under 600 bad. Its
   tooltip lists every lost point with its reason. Before the first answer it reads "Health
   --/1000"; when a later poll fails it keeps the last score and says since when.
3. **Navigation.** A small heading "WORKSPACE", then one link per page, each with a geometric icon.
   The order is fixed:

   | # | Link | Shown |
   |---|---|---|
   | 1 | Overview | always |
   | 2 | Roadmap | always (holds the Task roadmap, the Mission map and the Finished operations) |
   | 3 | Cockpit | always |
   | 4 | In-flight | always; a count badge with a small working orb shows tasks in flight |
   | 5 | Research | always |
   | 6 | Work & evidence | always; a count badge shows unfinished tasks |
   | 7 | Runs & logs | only when the project's domain profile turns on `runs` and serves `/hub/log.json` |
   | 8 | Live logs | only when the profile turns on `live` and serves `/live/log`; a badge counts running jobs |
   | 9 | Activity | always |
   | | *a 1 px divider line* | between Activity and Messages |
   | 10 | Messages | always |
   | 11 | Library | always |
   | 12 | Records | always |
   | 13 | Sessions | always |
   | 14 | Version control | always |

   A project's own pages (section 4.0) take the place their `nav` number gives them in this order.
   The current page has a tinted background, accent-coloured text and a 2 px accent rail at the
   sidebar's left edge. The link also carries `aria-current="page"`. The old Mission map link no
   longer exists; an old `#mission` address is rewritten to `#roadmap?view=mission`.
4. **Bottom links**, pushed to the foot of the sidebar, top to bottom: "All workspaces" with a left arrow
   (goes to `/`, the workspace hub; behind the host hub it goes to the host hub's root), "Sign out"
   (goes to `/logout`), the **connection row** (a 20 px orb and a label, see section 7.7) and the
   **version label** in 12 px mono type, "Alpaca 1.x.y". The label's tooltip reads "Release build",
   or "Development build at commit <hash>" on a development checkout.

### 2.3 Topbar

Left, centre and right groups share the free width; the two side groups take equal shares so the
centre group stays centred until a side needs more room. The gap between groups is 16 px.

- **Left: breadcrumb.** `<project name> / <page name>`. The project name is muted and comes from the
  record; before the first read it says "Workspace". The page name is medium weight. On phones a
  menu button (hamburger) sits at the start and opens the sidebar.
- **Centre, first: Find anything.** A button at least 220 px wide and 34 px high with a search icon,
  the words "Find anything" and a key cap "Ctrl K". Clicking it, or pressing Ctrl K (Cmd K on a
  Mac) anywhere, opens the search dialog (section 2.5). Below 1000 px of window width it shrinks
  to the icon alone; the shortcut still works.
- **Centre, second: date and time clock.** 12 px text with tabular figures, in the browser's locale,
  for example "Fri, 09 Oct 2026", a middle dot, then "14:05" (short weekday, two-digit day, short
  month, year; then hours and minutes). It repaints every 15 s. A 20 px orb appears at its left while a refresh runs
  (it "connects") or while new activity waits (it "listens"). When new activity is waiting the clock
  text is replaced by "New activity available" until the next refresh finishes (section 7.5).
  Phone width (700 px and below) keeps the time and drops the date and that notice: while the
  notice waits there, the time still shows and keeps ticking.
- **Right, first: theme switch.** A 58 by 30 px pill with a sun on the left and a moon on the right
  (section 3).
- **Right, second: the pulse orb.** The workspace activity orb, 56 px across (46 px at 600 px and
  below), set 26 px low (22 px on phones) so it hangs about 22 px below the topbar's bottom edge
  (about 17 px on phones) and its rings clear the page top. It is the **reload control**: a click, or
  Enter or Space when focused, reloads the current view from the record, and the orb plays its
  shapeshift as feedback (section 5, M2). It has a focus ring, `role="button"`, and a screen-reader
  caption of the counts. Its tooltip reads for example "2 tasks in flight / 1 job running. Evening
  colour; the pulse grows with concurrent work. Click to reload this view."

### 2.4 Main area and footer

The main area shows one page. A navigation first paints the page's loading sequence (section 5, M4)
and then the page, scrolled to the top (unless a filter box has focus). Every page opens with a
heading block: a small upper-case eyebrow, the page title (24 px), and one sentence of subtitle,
with actions at the right. Common parts used across pages:

- **Panels**: bordered white cards with a heading row and a body.
- **Badges**: small rounded state labels. Green for pass, done and complete; amber for blocked,
  stale and open; red for fail and error; blue for running, active and doing; grey otherwise.
  Running states carry a small working orb.
- **Toolbars**: a search box, selects and a count at the right. A search box asks once typing stops
  (900 ms after the last key) or at once on Enter, keeps the page on screen while the answer loads
  and asks nothing when the text is unchanged; a date updates the address after 450 ms and a select at
  once. A chip with a close mark clears a filter.
- **Empty states**: a small breathing orb, a title, one sentence saying what makes the page fill.
- **Error box**: a red-ruled box "This view could not be loaded" with the message and a "Try
  again" button that reloads the data.

### 2.5 Dialogs and toasts

- **Inspector** (a drawer). Slides in as a full-height panel from the right, 760 px wide at most
  (94 percent of the window on small screens). The reader variant, used for documents, is 1080 px at
  most. A sticky header carries an eyebrow label (WORK RECORD, DOCUMENT, DETAIL) and a close button.
  It opens when a task or outcome title is clicked anywhere in the Studio, or when a document is
  opened (`#library?path=...`). Esc or the close button closes it. While any dialog is open the page
  behind it does not scroll. Navigating closes it.
- **Command search.** A centred dialog 610 px wide at most, 15 percent of the window height from the
  top. One input, "Find a page, work item, or report...", and a list. With no text it lists every
  visible page. Typing filters pages by name and, once text is typed, adds up to 8 tasks whose title
  or id contains it, plus two rows "Search reports for ..." (Library) and "Search history for ..."
  (Activity). Clicking a row goes there and closes the dialog. Esc closes it.
- **Toast.** A dark pill centred 24 px above the bottom edge, shown for 3.5 s, used for short
  errors such as a failed "Show earlier".
- **Tooltips** (alpaca/web/tips.js, tips.css). Every tooltip in the Studio is the Studio's own tip, never
  the browser's, in the Rune dashboard's tip style: mono 11 px on a translucent surface with a hairline
  border, at most 360 px wide, just above the element (below when there is no room), kept inside the
  window, fading in over 80 ms with a 3 px rise. It shows
  320 ms after the pointer reaches an element with a title (an SVG element's title too), or at once
  for keyboard focus, and hides on leaving, a press, scrolling or Esc. The title moves into
  `data-tip` as the pointer arrives, so the browser never shows its own; an element named only by its
  title keeps that name as `aria-label`.

### 2.6 Narrow windows

| Window width | What changes |
|---|---|
| 1200 and below | sidebar 210 px, side padding 24 px |
| 1000 and below | the project name stays in the breadcrumb and ellipsizes only when space runs out (the page name shrinks first); Find anything becomes an icon; two-column layouts stack; on the Roadmap the operation title wraps so the page never scrolls sideways |
| 700 and below | sidebar leaves the screen (250 px, slides in over the page in 0.2 s when the menu button is pressed; any click outside it, Esc or a page change closes it); topbar 56 px; clock shows time only; footer padding shrinks |
| 600 and below | pulse orb 46 px |
| 360 and below | the page name hides; the project name ellipsizes and the time stays |

## 3. Theme

**Light is the default.** A visible switch in the topbar flips to dark. The choice is stored in the
browser (`localStorage` key `alpaca.theme`), so it is remembered per browser; a stored value
other than "dark" reads as light. Changing it in one tab changes every open Studio tab. The theme is
applied before the style sheets load, so the first frame already has the saved theme. The whole
page flips in one frame (no colour fade, because fading a large page lagged). Only the switch's
thumb animates: 0.45 s with a small overshoot; reduced motion turns that off. The switch is a native
button with `role="switch"`; its label says "Switch to dark theme" or "Switch to light theme". The
sign-in page and the workspace hub (section 4.15, 4.16) keep their own switch, their own keys
(`alpaca.login-theme`, `alpaca.hub-theme`) and default to dark.

**Fonts.** IBM Plex Sans (regular and medium files, self-hosted) for text and IBM Plex Mono for
code, ids and figures, with system fallbacks. Sizes: 12, 13, 14, 16, 20 and 24 px with line heights
16, 18, 20, 24, 28 and 32. Body text is 14 px. Spacing steps: 4, 8, 12, 16, 20, 24, 32 and 48 px.
Corner radii: 4, 6 and 10 px. Standard fade time: 150 ms.

**Colour tokens** (CSS variables on the root, `theme.css`). Names stay the same in both themes.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | #f6f6f4 | #16181b | page background |
| `--surface` | #ffffff | #1c1f23 | panels, topbar |
| `--surface-raised` | #ffffff | #212429 | dialogs |
| `--surface-muted` | #f1f1ee | #24282d | table heads, wells |
| `--surface-hover` | #ebebe7 | #2a2f35 | hover fill |
| `--text` | #22262b | #e4e6e8 | body text |
| `--text-strong` | #15181c | #f3f4f5 | titles |
| `--muted` | #5c636b | #a3a9b0 | secondary text |
| `--faint` | #80868d | #80878f | tertiary text |
| `--line` | #e3e3de | #30353b | borders |
| `--line-strong` | #d3d3cd | #3b4148 | stronger borders |
| `--control-border` | #8e939a | #6b727a | inputs, buttons |
| `--accent` | #3b5b86 | #93acd0 | links, active state |
| `--accent-soft` | #edf1f6 | #253040 | accent tint |
| `--text-on-accent` | #ffffff | #121417 | text on accent |
| `--focus` | #3b5b86 | #a9bddb | 2 px focus ring |
| `--green` / `--green-bg` | #2f6f4f / #edf4ef | #86c3a2 / #1e2c25 | pass, done, ready |
| `--amber` / `--amber-bg` | #83601f / #f7f1e5 | #d6b273 / #2f281b | waiting, blocked, notices |
| `--red` / `--red-bg` | #a3423f / #f8edec | #e3a09c / #382423 | fail, error |
| `--sidebar` | #f1f1ee | #191b1e | sidebar fill |
| `--sidebar-text` | #2b3036 | #dcdfe2 | sidebar text |
| `--sidebar-muted` | #5c636b | #a3a9b0 | sidebar secondary |
| `--sidebar-active` | #e5e5e0 | #262a2f | current link fill |
| `--sidebar-line` | #e0e0db | #2c3136 | sidebar borders |
| `--sidebar-accent` | #3b5b86 | #a9bddb | current link colour |
| `--chart-input` | #3b5b86 | #93acd0 | primary chart series |
| `--chart-cache` | #5b86ad | #7fb0d6 | cache series |
| `--chart-write` | #7a6aa3 | #b7a6dc | write series |
| `--chart-output` | #3f8a78 | #7fc4b0 | output series |
| `--chart-reason` | #9c7733 | #d6b273 | reasoning series |
| `--chart-cost` | #3b5b86 | #93acd0 | cost line |
| `--chart-area` | #3b5b8614 | #93acd01a | area fill |
| `--shadow-sm` | 0 1px 2px, 0 2px 6px at about 8 percent | 0 1px 2px at 25 percent | cards |
| `--shadow` | 0 8px 28px at 12 percent | 0 8px 28px at 40 percent | dialogs |
| `--backdrop` | #1b1f2366 | #000000a6 | dialog backdrop |
| `--toast-bg` / `--toast-text` | #2b3036 / #ffffff | #e4e6e8 / #16181b | toast |

State colours (`--st-doing`, `--st-ready`, `--st-waiting`, `--st-blocked`, `--st-done` and their
`-bg` forms) point at the accent, green, amber, red and muted tokens above, so they follow the theme.
The pulse orb's ink is `hsl(hue 72% 46%)` in light and `hsl(hue 80% 66%)` in dark.

## 4. Pages

Every page is reached from the sidebar or by its address. "Reads" names the server endpoints the
page uses (all under `/hub/` unless a path starts differently). "Refresh" says what the page does
when the record moves; section 7 explains the mechanisms. Where a page says "patched in place" the
new markup is merged into the old (section 5, M6) so scroll, focus and open sections stay. Where it
says "re-rendered" the page is built again and the scroll position is put back.

### 4.0 The page registry

Every page is one entry in `alpaca/web/pages.json` (its `about` key explains each field): id, label,
icon, nav slot, its modules and stylesheets, the server endpoints it reads, the live flags it reads
(section 7.2), whether a live refresh patches it in place, whether the 15 s timer refreshes it
(section 7.6), its loader steps, and `gate: "profile"` with the `profile_routes` it reads for Runs &
logs and Live logs. A `shell`
block names the frame's own stylesheets, modules, fonts and Arthur's files. The server builds the
asset allowlist and the reserved paths from it, and writes the stylesheet links (shell first, each
page's in registry order, `theme.css` last) and an inline copy of the registry into the hub page at
the `<!-- studio:pages -->` marker of `hub.html`; the hub reads that copy through `pages.js`, so the
first paint makes no extra request. Adding a built-in page is one registry entry, its files, and its
view in `hub.js`; `serve.py` and `hub.html` do not change.

**The project slot.** A project adds pages, cards and stylesheets of its own through its domain
profile's `web()` (`alpaca/profile.py`), with no edit under `alpaca/web/`. Its files sit in the
profile's own package, outside every mechanism path of `ALPACA-MANIFEST`, so `alpaca upgrade` never
touches them, and each is published under `assets` and served at `/hub/assets/profile/<name>`:

```python
def web(self):
    return {"pages": ["runs",                                # a built-in slot page, turned on
                      {"id": "bench", "label": "Bench", "module": "bench.js", "css": "bench.css",
                       "icon": "research", "nav": 5.5, "needs": []}],
            "cards": [{"id": "load", "title": "Load", "module": "load.js", "css": "load.css"}],
            "css": ["look.css"],                             # the project's own restyle
            "assets": {"bench.js": HERE + "/bench.js", "bench.css": HERE + "/bench.css", ...}}

def routes(self):                                            # the page's own data
    return {"/hub/bench.json": lambda root, query: ({"items": [...]}, 200)}
```

- *Pages.* A string turns on a built-in slot page: `runs` or `live`, each shown only when `routes()`
  also serves the registry's `profile_routes` for it (`/hub/log.json`, `/live/log`), since `serve.py`
  serves no run log of its own. An object adds a page: `id` (lower-case letters, digits, dashes),
  `label`, `module` and `css` (asset names), `icon` (an `icons.js` name, `file` when unknown), `nav`
  (its place among the built-in slots 1 to 14; after them when left out) and `needs` (the live flags
  of section 7.2 that refresh it; none by default). An id a built-in page or another entry holds,
  `lib`, `mission`, or a module that is not an asset, is left out and named in the profile's
  `web` hook error, which the cockpit shows (like a route that collides, `serve.py` `_profile_routes`).
  The overview's `profile` block carries the result: `pages` (the built-in ones turned on),
  `slot_pages` and `styles`.
- *The page module* exports `render(ctx)`, which returns the page's markup (or a promise of it), and
  may export `bind(ctx)`, called once the markup is in `#main`, and `stop()`, called when the reader
  leaves. A module that fails to load or throws shows the error view with Try again. The page reads
  its own data from its profile routes through `ctx.api`; it never adds data to the overview, which
  every page reads, so every page stays as fast as the overview allows.
- *ctx, version 1* (`ctx.v === 1`), the whole contract, so a page never imports a hub module:
  `page` (`{id, label}`), `params` (the address parameters after `?`), `overview` (the overview the
  hub already holds, read only), `api(path, params)` (a signed-in JSON read), `esc`, `icon(name)`,
  `heading(title, subtitle, action, eyebrow)`, `panel(title, body, action, subtitle)`,
  `href(page, params)`, `orb(state, size)`, `loader(kind, options)` and `patch(html, element)` (merge
  new markup into `#main` or the given element, keeping scroll and focus, section 5 M6). Markup with
  `data-icon="<name>"` gets its icon drawn. A later version adds keys and keeps these.
- *Stylesheets.* Every page and card `css` and the profile's own `css` list are linked after
  `theme.css` in the served hub page, in that order, so the project's sheet loads last and may
  restyle anything, shell tokens included. The template checks nothing about them (section 8.1).

`alpaca/tests/studio/sample_profile` is a working example (one page, its stylesheet and a restyle
sheet); `studio_fixture.add_sample_profile(root)` (or `studio_fixture.py <dir> --sample-profile`)
gives a project that profile, and `alpaca/tests/test_studio_slot.py` checks it in the browser. The
standard fixture carries no profile, so the baseline and motion checks keep the default Studio.

### 4.1 Cockpit (`#cockpit`)

*Shows.* The live engineering view: what is moving, what finished, what needs attention. Top to
bottom: a heading with the label "Refreshes every 15s" (or "Updates paused"), a **Pause/Resume**
button and a **Focus view** button; any profile problem notice; the **focus card** (the current
assignment, or the next open task, with title, purpose, assigned session, time since the last task
record and, when runs are on, the active run) beside the **operation progress ring** (tasks done in
the current op); a **Research runs** card; a compact **Mission map** card (one tile per sector);
**Parallel sessions** (one card per live work session with its latest ask and the tasks it holds;
a session with no claimed task says so; a lapsed lease is named); a strip of up to five **stat links**
(unfinished tasks, active runs when runs are on, outcomes needing attention when a runbook has
outcomes, open operations, last meaningful activity); any **profile cards**; then two columns: left,
**Execution** (only when runs are on) and **Recently completed** (last three done tasks with their
result and proof link); right, **Next engineering action**, **Latest activity** (six progress events)
and **Capture health**. A **host load panel** closes the page.

*Reads.* `overview.json`, `runs.json`, `history.json?limit=6&kind=progress`, `tasks.json?ids=...` (the assignment card, the latest completions and the claimed tasks); then, without blocking
the first paint, `research.json`, `mission.json`, `/board/data.json`, `/data.json`; the host panel
polls `system.json` every 3 s; profile card modules load from `/hub/assets/profile/`.

*Can do.* Click a task title to open its record in the inspector. Pause stops automatic refresh and
repaint until Resume. Focus view hides the sidebar and footer and widens the page (up to 1900 px)
for a wall display; Esc or the button leaves it; it only exists on the Cockpit.

*Empty.* With no open work the focus card says "No unfinished task is recorded." Panels that find
no data say so in one line ("No work session is live.", "No task completion is recorded yet.").
A panel that fails to load says it could not be refreshed and keeps the last snapshot.

*Refresh.* Every 15 s, and on pushed changes, the page is re-read and **re-rendered only if the text
changed** (ignoring "5m ago" labels), or at least every 5 minutes so those labels stay current; the
Research, Mission and Parallel sessions panels are patched in place. A refresh never runs while
Pause is on, a field has focus or a dialog is open.

### 4.2 Overview (`#overview`)

*Shows.* "A clear view of the work." with a button "Explore activity". A strip of metric tiles (Work
in progress, Current acceptance when the project has runbook outcomes, Completed tasks, Open
operations, Completed this week, Active runs when runs are on). "Work in focus" lists the current
assignments and the four newest open ops with progress bars. A bar chart of task completions per day
(latest 14 days with records). "Stage readiness" lists a profile's stages with pass or open marks (only
with a profile). Then three panels: Recent work (five events), Capture status, Latest reports (four
documents).

*Reads.* `overview.json`, `history.json?limit=5`, `documents.json?limit=4`, `runs.json`.

*Can do.* Open an op in Work & evidence, open a report in the Library reader, open a task.

*Empty.* "No open operations" with a breathing orb; "No reports yet".

*Refresh.* Every 15 s and on pushed changes; re-rendered with the scroll position put back.

### 4.3 Roadmap, task roadmap view (`#roadmap`)

*Shows.* Three tabs at the top, **Task roadmap**, **Mission map** and **Finished operations** (with the
number of closed ops). The task view has a heading
("Roadmap", with a button "Open checklist"), then one **operation map** for every pinned open op: a
collapsible panel headed "Operation map Op12 - short title" with a strip of **phases** (each with a
done bar, state "Complete", "Current phase", "Open" or "Not opened"), who is driving the op (a chip
per session holding open work, with its autodrive level), and notes (phase door, work waiting on the
owner, blocked work). Below it, **Work by area** for the whole workspace: groups (by work area, then
by workstream) of **task cards** joined by arrows that show prerequisites. A card shows its id, state
label (In progress, Ready, Waiting on prerequisites, Needs approval, Waiting for resources, Blocked, Assignment expired, Failed, Skipped, Done, Needs organization),
the title, who has claimed it and a **...** button for details (op and phase, prerequisites,
reasons, "Open chat", "Trace path"). Research runs appear as one card each in a "Research runs"
group.

*Reads.* `overview.json` only (the plan comes inside it).

*Can do.* Pick an operation from a searchable menu (or "All operations"); find a chat to highlight
the work assigned to it; click "Trace" on a card to highlight its prerequisites and dependents (the
rest dims to 48 percent and the connecting arrows turn accent coloured); "Clear focus" undoes it;
fold or open groups; **Open all**, **Summary**, **Close all**; filter by work area; switch between
**Active work** and **Full plan** (done work is folded away by default); collapse the whole panel.
Click a title to open the task record.

*Empty.* "Pin an operation with bin/alpaca op pin OP to show its phases here." and "No tasks have been
recorded yet."

*Refresh.* Every 15 s and on pushed changes the page is re-rendered with the scroll position put
back. Fold state, opened card details, focus, chat, filter, scroll and panel collapse are kept in
the browser's session storage under `alpaca.roadmap.*`, so a refresh or a reload keeps them.

### 4.3a Roadmap, finished operations (`#roadmap?view=finished`)

*Shows.* Every closed operation as a card, newest close first: the op id and short title, "Closed"
with the date and time, "N of M tasks done", how long it ran (opened to closed), and a **Why it
closed** fold holding the basis recorded by `alpaca op close OP --basis`. A card's title opens that op
(`#roadmap?view=finished&op=op-12`): its full intent, the card, its **Done when** line, and its own
task roadmap with the done work shown, so what an op delivered stays one click from the roadmap.

*Reads.* `overview.json` only: each op carries `opened`, `closed` and `close_basis` (the basis of its
newest `op-close` event).

*Can do.* Open a finished op; "All finished operations" goes back; "Back to the task roadmap".

*Empty.* "No finished operations yet" with the close command.

*Refresh.* As the task view.

### 4.4 Roadmap, mission view (`#roadmap?view=mission`)

*Shows.* The mission map: the system's components in sectors, with their state, the work ahead and
what a change affects. Heading "Mission map"; a scope sentence; counts; a "Ready next" strip; a
toolbar with three modes (**Development**, **Components**, **Change impact**), a component search, a
concern filter, a history snapshot picker ("Current system" or a saved date), Expand all and
Collapse all, "All links", "Suggested links" and an impact-depth picker. Sectors are folds holding
**component cards** with a state chip: Verified, Failing, Needs recheck, Not checked, Blocked. Selecting
a card opens a detail column: purpose, state and reason, linked work, recorded checks (with "Read check
evidence"), relationships and change history. Below sits the **Development plan** (the roadmap for the
tasks linked to components; a check box limits it to the selected component) and a "Map coverage"
fold listing unmapped files.

*Reads.* `mission.json` (optionally `?at=<snapshot>`); `mission-evidence.json` when evidence is opened
(needs a sign-in). Address parameters: `node`, `group`, `at`. An old `#mission` address is rewritten.

*Can do.* Everything in the toolbar; select a component; follow links to work, sessions and proofs.
Compact version: the Cockpit card shows one tile per sector linking to the matching sector here.

*Empty.* "Map data is unavailable. Refresh to try again." A project with no configured map shows a
"Starter map" notice naming `alpaca mission init`. A saved snapshot shows a notice that it does not
certify today's inputs.

*Refresh.* Every 15 s and on pushed changes; re-rendered, and the selection, search and fold state survive. Links between
components are drawn as arrows over the cards and redrawn on resize; they are hidden at 600 px and
below.

### 4.5 In-flight (`#inflight`)

*Shows.* One card for every task in progress whose claim lease is still live, newest activity first,
under a line "N in flight. Updated 14:05, every 15 seconds. Every line is read from the record and
the claiming session's transcripts; nothing is generated." A card carries: id, op, phase and
workstream; the title; the autodrive level chip; running time; claim time; the claiming session link;
lease end; a **progress** bar (from the task's own phases when it has them, otherwise a labelled
"no percentage"); a **now** line; **milestones**; six figures (responses, tokens, estimated cost, tool
calls, subagents, workflow runs); the **activity feed** ("What it is doing": time, kind, text); and a
side column with direction (mode, files focused, test results), **off-track signals** (loops, runaway
token bursts, friction, silence over 10 minutes, idle gaps), the plan (prerequisites, previous and
next step, what it unblocks), workflow fan-out, status notes, owner instructions and a fold with the
task statement and contract.

*Reads.* `inflight.json` (and `inflight.json?task=...&before=...` for earlier feed lines), `overview.json`.

*Can do.* "Show earlier" loads older feed lines for one task and keeps them across refreshes. Click a
title for the task record. `#inflight?task=t-123` scrolls to that task's card and gives it a focus
ring.

*Empty.* "Nothing is in flight. No task is in progress with a live lease. A claimed task appears here
with its activity."

*Refresh.* Every 15 s and on pushed changes; cards are patched in place, so a reader keeps their
place. A card wears the live **border beam**; one with an off-track signal or a lease that lapses
within 10 minutes wears the red beam.

### 4.6 Research (`#research`)

*Shows.* The run list: heading "Research runs"; filters Find run, Run type (All, Research, Control)
and Mode (All, Improve, Repair), with "N of M runs"; groups **In progress**, **Needs attention**, **Past
runs** and **Control runs**, each a set of run cards with a state chip (a running run carries a
working orb). One run (`#research?run=<id>`): heading with state and a back link; progress and round
count; the champion; a candidate reader; a banner for the run's state (Running, Stopped, Failed and
so on); repair findings; a chart of the metric by round (a missing value is never drawn as zero; the
table beside it is the text form); candidate decisions by round; lineage; a fold with the full attempt
table; a fold with run settings.

*Reads.* `research.json` (`?run=<id>` for one run), `overview.json`.

*Can do.* Filter runs; open a run or a candidate; filter attempts by outcome; open evidence in the
Library reader.

*Empty.* "No research run is recorded. Start one with /autoresearch or bin/alpaca research start
--runbook PATH." With filters that match nothing: "No matching runs" and a "Clear filters" button.

*Refresh.* Every 15 s and on pushed changes; the page is read again and re-rendered from the record, scroll put back.

### 4.7 Work & evidence (`#work`)

*Shows.* "A numbered checklist of work, methods, results and proof." Tabs **Tasks** and (when the
project has runbook outcomes) **Runbook outcomes**, each with a count. A segmented state bar and a
completions-per-day chart. A toolbar: search, status select, operation select (hidden in the
checklist and roadmap layouts), a stage chip, a **view switch** (tasks: Checklist, Roadmap, History,
Cards, Table; outcomes: Timeline, Table) and the count. Checklist and Roadmap show the roadmap
component of 4.3 scoped to the filters; History is a day-grouped timeline; Cards show what each task
delivered with a fold for its method; Table lists number, item, state, what was delivered,
completion and session, and proof link.

*Reads.* `overview.json`, and `tasks.json` for the card, timeline and table layouts and for a search (the task text, t-591). Address parameters: `tab`, `op`, `stage`, `status`, `q`, `layout`.

*Can do.* Click a task to open the record in the inspector: facts, plan position, contract (input,
expected output, done bar, fail cases), purpose, report sections, proof links, history, related
messages and activity. Click an outcome for its method, observed result and evidence.

*Empty.* "No matching work. Change the search or filters to see other work items."

*Refresh.* On pushed changes the page is re-rendered with the scroll position restored; it does not
run on the 15 s timer.

### 4.8 Runs & logs (`#runs`, only with a profile)

*Shows.* "Follow execution from recorded attempts to stage results and the original output." A line
about running jobs (links to Live logs), a **design and stage matrix** (latest attempt for every pair;
click a cell to open its log), KPI tiles and charts, a table of runs (design, stage, result, start,
duration). One run (`#runs?run=<id>` or `?receipt=<id>`) shows a run dashboard: stage steps, a **log
viewer** (follow, search, jump to first error, save a copy, filter by flagged lines, tabs Errors,
Warnings, Notes), the **contract** cards (Input, Expected output, Done bar, Fail cases) and the
**agent review** of the log. The log scan only points at lines; the receipt's verdict stays the result.

*Reads.* `runs.json`, `overview.json`; the profile's routes `log.json`, `logscan.json`, `contract.json`
and `logreview.json`.

*Empty.* "No runs match. Run receipts will appear here as stages are executed." The page does not
exist in the sidebar unless the project's profile turns it on and serves `log.json`; a project with
no profile does not show it.

*Refresh.* On pushed changes, re-rendered; a followed log reads new bytes by itself while open.

### 4.9 Live logs (`#live`, only with a profile)

*Shows.* "Every running job, side by side." One tile per running job, each following the job's log
across its stages, with a layout select (Auto, Side by side, Stacked, Two columns), **Pause all**,
**Clear finished**, a strip of pinned jobs and a count ("N jobs running"). `#live?jobs=a,b` pins
finished jobs for replay.

*Reads.* the profile's `/live/log` and `/live/telemetry.json`, plus `logscan.json`, `contract.json`,
`overview.json`, `runs.json`. It polls on its own timers (about every 2 s for log bytes, 10 s for the
others) and never takes part in the 15 s refresh.

*Empty.* "No job is running now. Tiles appear here as soon as a job starts, one per job." with
buttons to pin recent jobs. The page exists only when the profile turns it on and serves `/live/log`.

### 4.10 Activity (`#activity`)

*Shows.* "Task, op and run progress from the project record, on a live clock." A **timeline panel**
with two views, **Live** (events placed by time in lanes; windows Last hour, Last 6 hours, Last 24
hours) and **By day** (a chosen day with day-by-hour counts, default today). Below, a toolbar
(search, kind select with six choices, window select, chips for a session or ref filter, and a
count) and the **event list** as a vertical timeline: a "Now" marker at the head, a day marker
wherever the date changes, a time and relative age at the left, a dot coloured by lane, the event
sentence, its kind, its ref and session links, and a fold "View recorded context" with the raw data.

*Reads.* `history.json` with `q`, `kind`, `session`, `ref`, `limit`, `before`, `day`.

*Can do.* Click a timeline dot to scroll to its row, which flashes (1.6 s fade). "Load earlier
events" pages back 50 at a time. Default kind is "Task, op and run progress"; other choices widen it.

*Empty.* "No matching events. Try another term or switch to all recorded events."

*Refresh.* Every 15 s the newest events are added at the top and the pages already loaded stay (the
list is re-rendered with the scroll position put back). A pushed change re-reads the first page.

### 4.11 Messages (`#messages`)

*Shows.* "Reports, decisions, and handoffs left in the shared record for people and agents." A bar
chart of message kinds in the filtered set, a toolbar (search, kind, recipient, count), then one card
per message: a two-letter avatar, sender, "to <recipient>", a kind badge (a question carries a
listening orb), time, a 650-character preview with a fold "Read full message", and links to the ref's
activity and to the session.

*Reads.* `overview.json`.

*Empty.* "No matching messages. Agent reports and handoffs appear here when they are recorded."

*Refresh.* On pushed changes, re-rendered with scroll kept.

### 4.12 Library (`#library`)

*Shows.* "Reports, runbooks and project knowledge, organized for reading." Tabs: Reports & knowledge,
Sealed work & proof, Runbooks, Decisions & plans, Tool documentation, All documents. A format chart,
a toolbar (search, format select, count) and a table (document, collection badge, format, updated).
`#library?path=<file>` opens the **reader** drawer: the path, a badge for the format, **Save copy**,
and for a sealed proof an "At a glance" summary and a section navigation. Markdown is rendered from
escaped text only; HTML reports are shown in a sandboxed frame with scripts removed. A document longer
than the server limit shows a notice that it is partly displayed.

*Reads.* `documents.json` (`q`, `category`, `ext`, `offset`), `document.json?path=...` (needs a sign-in),
`/files/read?path=...` for other file types: the generic text extensions
(`FILES_TEXT_EXTS` in `alpaca/serve.py`) plus any the profile names in `paths()["text_exts"]`.

*Can do.* Load more documents (paged); open, read and save a document; follow links between documents.

*Empty.* "No documents match. Change the collection or search to find a report."

*Refresh.* The `library` flag refreshes it on pushed changes (re-rendered, scroll kept).

### 4.12a Records (`#records`)

*Shows.* The project's official record and its wiki, read-only, in four tabs with their counts:
**Operations** (every op: id, short title and intent, Open or Closed, opened and closed times, done
tasks of all, and a **Briefing** link; an open op links to its checklist, a closed one to its page on
Roadmap's Finished operations), **Decisions** (each recorded decision, newest first: its kind, time and
id, the choice and its consequence, and a **Context and options** fold with the chosen option marked),
**Sealed proofs** (every done task with a sealed proof, newest first: the task, which opens its record,
its title and op, when it was done, and **Read report**, which opens the report in the Library reader)
and **Wiki** (what the project wiki holds: serving state, documents, active blocks, nodes, links, where
its knowledge time comes from and quarantined captures; and a box that takes an op or task id and
shows the wiki's cited briefing of it, one source per line, with the gaps it found). Right below the
wiki's summary sits the **vault seal** (alpaca/web/ventry.js, after the owner's Rune dashboard entry):
in a box of its own, steel doors with Futhark columns and seam bolts, a combination dial inside three
glyph rings, the record's counts typed out (operations, decisions, sealed proofs, the wiki's documents
and links) and twelve key slots rolling hex. The rings align in about a second and the seal waits at
ENGAGE THE SEAL for as long as it takes; it never opens itself. Pressing the dial (or Enter, or a
double-click) grants access: the rings spin up to a blur, open inner to outer, the bolts draw, the
doors part and the vault opens as the **evidence graph** (`#records?section=wiki&vault=open`,
alpaca/web/evgraph.js), with no second entry. Under reduced motion the box is the plain **Open vault**
card. The graph has fifteen colour-coded columns, left to right:
Operations, Tasks, Decisions, Sealed proofs, four evidence kinds (record events, logs and outputs, code
and tests, documents and other files), Review cards, Phase doors, Sessions, Messages, Wiki assertions,
Wiki claims (a contradicted one in red) and Wiki sources. Each entry is a short mark stacked down its
column and every link a curve between two columns (cited by, proves, part of, named in, reviews, door
of, worked on, mentions, subject of, object of, source of, quoted in). Selecting an entry lights its
chain, everything it rests on and everything resting on it, labels the lit entries and opens a side
panel: the item's facts with the record event behind them, its links as buttons that step through the
graph, and what the wiki records about it with each cited quote and source document. **Browse**, a
tray of cards under the toolbar (folded until **Browse** is pressed, and kept so), narrows the record
tier by tier: it opens on every operation, newest
first; a card selects its item and opens the items linked to it, grouped by kind (an operation's tasks,
decisions, messages, review cards, phase doors and wiki items; a task's sealed proofs, sessions,
messages and decisions; a proof's evidence); **Back** steps up one tier and the path of crumbs above
it jumps back further. A tier shows
60 cards per kind, and its one filter box narrows it; at the first tier the box finds any item in the
vault (it too waits for typing to stop; Enter selects the first match) and rings the matches. **Hide
list** folds the tray away. The legend's chips
highlight a sector: a pressed chip lights its column's links and makes only them fire, the rest stays
on screen dimmed, and **Clear highlight** ends it; nothing is ever hidden. Scrolling on the graph zooms
its columns, dragging moves them, **Fit** resets. **Firing** (0 to 100 percent) sets the share of links
that fire, each on its own beat: a pulse with a short tail runs along the link from its left end to its
right end, from operations toward the wiki, and the mark it reaches flashes; at 100 percent every link
fires. **Speed** (0.25x to 4x) sets the pulse rate, and **Fire all** jumps to 100 percent (it reads
**Calm** there and returns to the earlier share). **Brain** draws the same graph as a 3D brain inside a
wireframe of both hemispheres, the cerebellum and the brainstem: each column is a region (sessions sit
deep in the basal ganglia), neurons fill each region's volume, axons carry the pulses. It is drawn
like the H01 map of human cortex (Harvard and Google, Science 2024) on an imaging field that follows
the page theme (light with the page palette, or dark with the vivid one): each item a small lit orb
(a soft halo and the shaded neuron cell with its forked dendrites, in four shapes and tints per
colour; the smallest are dots) with one to three thin neurites that wander off in 3D, every link a
long thin axon thread that bends on its way, blended so crossings glow in the dark theme.
**Brightness** (10 to 200 percent, 70 at first, kept per viewer) scales the axons, neurites, halos,
orbs and flashes. Somas and neurites grow with link count and zoom. While
the brain turns, pans or zooms it draws a lighter sketch at half resolution (one axon in six as
straight strokes, neurites only on the near side, neuron cells on the near side only, the rest as dots, flashes as batched dots) and paints
in full 600 ms after it rests, in slices into a spare picture that is swapped in when done, with the
axons at half resolution (auto-rotate holds still while the viewer pans, zooms or holds a key); its pixel density is capped at 1.5x. Drag turns it,
Shift-drag, right-drag, the arrow keys or WASD (held, they glide) move it, Shift+arrows turn it, the
wheel or a pinch zooms (pan and zoom reuse the finished picture until input rests), **Auto-rotate** and **Turn** (0x to 4x)
set the spin, a pressed legend chip turns its region to the front, **Full screen** fills the screen.
A double-click anywhere on the graph sends a shockwave from the cursor: rings run out, the marks or
neurons on the front light up, and in the brain the neurons are pushed out and spring back.
These settings are kept in the viewer's browser. An open vault is never rebuilt by the live refresh, so
its zoom, turn and selection hold while the record changes. **Close vault** returns to the search
mode. The selected item stays in the address (`&item=`).

*Reads.* `/hub/records.json?section=ops|decisions|proofs&q=` (`hub.records`, the record through
`connect_readonly`); on the Wiki tab `/hub/wiki-status.json` (`hub.wiki_status`: a summary of
`alpaca.wiki.service.status`, read once a minute because it reads the whole store) and
`/hub/wiki-briefing.json?op=|task=` (`hub.wiki_briefing`: `service.briefing`, servable sources only,
never stored); with the vault open `/hub/graph.json` (`hub.evidence_graph`: the record through
`connect_readonly` and `alpaca.wiki.service.graph_claims`, servable claims only, one build until the
record moves or a minute passes) and, for a selected item, `/hub/graph-item.json?id=`
(`hub.graph_item`, with `service.graph_assertions`). The wiki and graph routes need the web login the
reports need (403 without one).

*Can do.* Switch tabs; search a tab (the text of every field); open an op, a task record or a proof
report; ask the wiki about an op or a task; open and close the vault, find, select and trace an item.
Nothing on the page writes.

*Empty.* "No operations recorded", "No decisions recorded", "No sealed proofs yet"; the Wiki tab says
there is no store yet ("bin/alpaca wiki recover builds it").

*Refresh.* On opening and on a search; no timer (the wiki status is costly). An open vault keeps its
selection and zoom through a live refresh. Motion: a lit chain always pulses; with nothing selected
the Firing share fires (30 percent at first). Reduced motion starts at 0 percent with still frames
until the viewer moves a slider.

### 4.13 Sessions (`#sessions`)

*Shows (index).* "Understand the cost, context and actions behind the work." Above the list sits the
**token and cost board**: tiles (tokens, top session's share, subagents, span), a heat chart of
session timing, "How hard each session pushed, and what it cost", per-session breakdowns, off-track
signals, cost per outcome, collisions and hook overhead, and the same numbers as a table. Window chips
select 24 hours (default), 7 days or All. The list has a toolbar: search, **Agent** (All, Claude only,
Codex only, Other or unknown), **Sort** (Newest first, Oldest first, Recently active, Most cost, Least
cost, Most tokens, Most responses), and the **date range**: a **From** and a **To** date picker (no
day after today), and a "Clear dates" button. A summary strip (sessions analyzed, estimated API cost,
tokens, model responses) and a table follow; sessions without captured responses sit in a fold.

*Date range rules.* The range is in the address (`#sessions?from=2026-10-01&to=2026-10-09`). A
session is listed when its first-to-last activity overlaps the chosen days; the board counts the
requests made inside the days, so a picked range replaces the window chip. With only From, the range
runs to today; with only To, it shows that one day. A day after today moves to today and a visible
note says so. A From after To is refused with a visible message and no range applies. The server
refuses a range longer than ten years. Days are the viewer's own local days.

*Shows (one session, `#sessions?sid=<id>`).* A summary (title, id, model responses, tool calls, child
records, captured span), links to activity and messages, a session switcher, and seven tabs:
Overview, Costs & tokens, Context window, Actions & hooks, Agent crew, Conversation & tools, Work &
reports. `child=<id>` opens a subagent's own record; `around=<line>` opens the conversation at a line.

*Reads.* `/data.json`, `analytics-index.json`, `burn.json` (with `window=` or epoch `start`/`end`),
`session.json`, `analytics.json`, `analytics-children.json`; `overview.json` for the Work & reports tab.

*Empty.* "No model response measurements are available." A session id with no capture: "No captured
session is registered" with links to its activity. The first scan of a large record shows a loader
saying it can take a minute while other pages stay available.

*Refresh.* The `sessions` and `analytics` flags: the index and an open session are patched in place.
The Conversation tab is being read, so it is not refreshed automatically; the "New activity available"
notice stays until the reader reloads with the pulse orb.

### 4.14 Version control (`#vcs`)

*Shows.* A read-only picture of the repository: "Explore branch history, inspect commits and review
local work." The repository path and "Read-only. Updated 14:05". A section navigation (Branches,
Worktrees, Local changes, Activity, Remotes). The **commit graph**: lanes and dots, window tabs 1, 7
(default), 30 and 90 days (`#vcs?days=N`), at most 400 commits, a search box over the loaded
history (Enter jumps to the next match), and a button to expand all loaded commits. Selecting a
commit shows its details; task and op ids in a message are links. Then a branch overview with
workspace signals (worktrees with changes, locked or prunable worktrees, diverged upstreams, stash
entries), and folds for Worktrees, Local changes (open by default), Git activity (from the reflogs), Remotes & releases, and Git guard
history (commands the guard refused in the last 30 days).

The **Worktrees** fold opens with an overall line: how many worktrees exist, the uncommitted files
across those whose status is known, how many are not loaded yet, and how many could not be read. Then
the live part, each row with branch, head, ahead and behind main, uncommitted count with when it was
read, and session: the **Main tree**, the **Most recent** five (a live session in it first, then the
newest of: its files moved, its index, HEAD or top directory changed, its HEAD commit) and the
**Largest** five by last known uncommitted count (a tree already among the recent is not repeated; the
next largest fills its place). Every other worktree sits in a closed fold, "Other worktrees (N)", by
path and branch, with its last known count (or "not loaded") and a **Load status** button. Pressing it
reads that one worktree now and keeps it live for 2 minutes.

Why: one git status costs about 0.1 s on a warm tree and can cost seconds on a tree with stale index
data, so reading every worktree on every pass grows with the number of worktrees (204 of them once
cost about 9 CPU s per pass). The live set is at most eleven trees plus the ones a viewer opened,
so the cost stays bounded however many worktrees exist.

*Reads.* `vcs.json?days=N`. Nothing is fetched from a remote. `vcs-status.json` is polled every 5 s
while the page is visible: it returns the cached status of the live worktrees (main, most recent,
largest, opened) and the overall line, and runs no git status itself. `vcs-tree.json?path=<worktree>`
is the Load status button: it accepts only a path that is exactly one of the repository's worktrees
(anything else is a 404 and runs nothing), reads it with a 4 s limit and returns its row. A sweeper
thread (started by any of these requests, ended 2 minutes after the last) reads only the live set:
the hot ones every tick (live session, or status, HEAD or top directory changed within 30 min) and
the rest in a rolling pass of about 70 s. Beside it a ranking lane reads at most one other worktree
per tick, one never read or last read over 15 minutes ago, so the largest stay roughly known without
a full pass. A snapshot build reads the live set too; any other row shows its cached count or none.

*Empty.* "The main checkout has no uncommitted change." "No tags." "None." A repository with no
commit yet (`alpaca new` runs `git init` and commits nothing) shows its branch and "No commits in
this window. Try a wider time window."; a project folder with no repository (`alpaca new --no-git`)
shows "No git repository" with the folder path, and `vcs.json` answers `{"repository": false}`.

*Refresh.* Every 15 s and on pushed changes; patched in place, keeping the selected commit, the search
text, the expanded state and open folds.

### 4.15 Workspace hub (`/`)

*Shows.* The project's front door, in the dark vault look: a wordmark "Alpaca Hub", the line
"Select an operations workspace", and one **tile** per workspace known to this host (`/workspaces.json`).
A tile shows the workspace's initial, name (its registry name, else the `name` in `project.yaml`, else
the folder name), kind and project; a live badge ("N live", "idle", or "up"
or "down" for another instance); the current open operation with a progress bar and "N of M tasks done"
or "rows discharged"; four figures (Open ops, Doing, Queued, Done); the next action; and "Open
workspace" with the autodrive level and "updated N min ago". Corner chips: Theme and Sign out.

*Reads.* `/workspaces.json`, then each tile's own summary (`/board/data.json` for this workspace;
`/workspaces/summary.json` for another instance, which gives reachability only). A tile refreshes every
15 s while the tab is visible.

*Can do.* Click a tile: a "takeover" overlay plays (OPENING, three stages, a progress bar of the real
page reads, "WORKSPACE READY") and the Studio opens with its first reads already fetched. A Ctrl,
Cmd or Shift click or a middle click opens normally. With fewer than two workspaces a ghost tile
explains `alpaca workspace add`. Behind the host hub's path router this page forwards to the host hub.

*Empty and errors.* Before the summary arrives the tile reads "Reading the record". Another instance
that is down: "Workspace not answering. It may be stopped." A failed list: "Could not load the
workspace list: ..." A lapsed session sends the browser to the sign-in page.

### 4.16 Sign-in (`/login`)

*Shows.* A dark page with a rotating coastline globe, a scrolling relay feed and a system-log ticker
(all decoration, hidden from screen readers), and a centred card: "Operations access", the line
"Restricted terminal. Authorized personnel only.", an **Access code** field (digits; ring graphics
around the card grow with every character typed, up to 12 at a full code), an optional **User** field
(shown only when the server asks for a user name) and an **Unlock** button with a spinning orb. When
the server uses an authenticator app the field asks for the 6-digit code.

*Reads.* `/login/info.json` (whether login is configured, whether a user name and a code are needed,
whether the browser is already signed in), then posts to `/auth/login`.

*Can do.* Enter the code and press Unlock. A wrong code shakes the card for 0.42 s and shows the
server's message; repeated failures are throttled. A server with no login set up says "No web login
is configured on this server. Set .alpaca/files-auth first." On success a "takeover" plays (five
stages: ESTABLISHING SECURE CHANNEL, VERIFYING ACCESS CODE, SIGNING SESSION TOKEN, LOADING WORKSPACE
REGISTRY, AUTHORIZING CLEARANCE; then ACCESS GRANTED and the vault opens) while the next page's reads
are fetched, and the browser moves on to the page it asked for. The session lasts 12 hours. An
already signed-in browser skips the form. The `next` address is accepted only if it is a plain path
on this host.

## 5. Animations

Nothing animates for its own sake: each motion says a state. With the system setting "reduce
motion", the sheet rule in `hub.css` switches off every CSS animation and transition, and each canvas
or script animation has its own still form, given in the last column.

| Id | Animation | Trigger | Timing | Reduced motion |
|---|---|---|---|---|
| M1 | **Pulse orb cycle** | work running: in-flight tasks plus running jobs above zero | the orb steps through searching, solving, connecting, weaving, composing, shaping, working, one state every 7 s (49 s a loop); with no work it holds "breathing" | cycle still runs, orb draws one still frame |
| M1 | **Ripples** | always | 2 to 4 rings spread from the orb's edge and fade; the period is 6 s at no work and shortens by 0.35 s per concurrent item to 3.2 s at 8; waves: 2 at none, 3 at 1 to 3, 4 from 4; glow strength 0.16 plus 0.08 per item | one static ring, no sparks |
| M1 | **Sparks** | always | 4 dots at no work, then 6 plus 2 per item up to 18; each flies outward on its own angle and fades over 0.9 of the ripple period | hidden |
| M1 | **Hue by time of day** | clock | the hue is 230 degrees at midnight and turns once round the wheel a day (deep blue at midnight, magenta at dawn, red mid-morning, gold at noon, green in the afternoon, teal and cyan in the evening); the clock is read in 5-minute slots, 1.25 degrees a step; a new slot fades in over 30 s (20 ticks of 1.5 s) by the short way round; the tooltip names the period (Night to 05:00, Dawn to 08:00, Morning to 12:00, Afternoon to 17:00, Evening to 21:00, Night) | the hue still changes |
| M2 | **Click shapeshift** | click, Enter or Space on the orb (it also reloads the view) | 0.3 s rise, hold to 2.6 s, then an ease-out back to rest over 1.6 s, 4.2 s in all; during the hold the orb and its rings become a drifting blob outline (corner radii within 28 to 72 percent), rotate up to 16 degrees, squash and stretch up to 16 percent and swing the hue up to 24 degrees; it jumps to another of 9 particle shapes every 0.42 s; the workspace state returns 0.48 s into the ease-out where the motion hides the swap; every changed value returns to rest, so nothing snaps; a click while moving continues from the current strength | one shape change, no movement, back after 2.6 s |
| M3 | **Thinking orbs** (`<alpaca-orb>`) | wherever a state needs a mark: running badges (working), empty states (breathing), the In-flight count, the connection row (listening when live, connecting when not), the refresh indicator, a question message (listening) | dotted spheres in three presets (20, 32, 64 px); the states are working, searching, solving, listening, connecting, weaving, composing, breathing, shaping; one shared animation frame loop, an orb off screen is skipped, they take the text colour and repaint on a theme change | one still frame |
| M4 | **Loader sequences** | first paint, and every page change until the page is read | large orbs (96 px, 48 px compact) join one at a time every 1.1 s, sliding in over 0.55 s from the right; the earlier ones settle small and faint (0.5 s); one line of text per orb fades in (0.35 s) and out (0.25 s); a gradient beam sweeps under the line every 1.6 s; the last line stays until the page replaces it; each page has its own steps (for example Cockpit: Searching the project record, Reading tasks, proofs and runs, Checking what is running, Composing the cockpit, then "Still reading; a large record takes a few seconds") | all orbs at rest, only the last line shown, beam still |
| M5 | **Border beams** | a card for live work: a roadmap or operation-map card in progress, an In-flight card; the red variant for work that needs attention (owner decision, blocked, failed, lapsed assignment) or an In-flight card with an off-track signal | a coloured comet runs round the card border once every 3.2 s (2.4 s for red), fades in over 0.6 s, its hue drifts back and forth over 12 s | a still ring at a fixed angle |
| M6 | **Morph patching** | an automatic refresh of the In-flight cards, the Version control board, the Sessions index, burn board and open session, and the lower Cockpit panels | the new markup is merged into the page: unchanged nodes are kept, rows are matched by key, inserted or removed rows are found within 8 siblings; scroll, focus, open folds and the values of fields are never reset | not animated |
| M7 to M9 | **Arthur** | see section 6 | poses, reactions, gaze, tricks and pacing | poses show their first frame; pacing plays in place |
| M10 | **Roadmap transitions** | folding a group, selecting a card, resizing | the fold arrow turns 90 degrees and cards fade their opacity and shadow in 150 ms; arrows between cards are routed round the cards at right angles and redrawn after every fold, selection, panel collapse or resize; fold state and focus survive a refresh | transitions off |
| M11 | **Mission map draw** | page open, selection, fold, resize | links redrawn as arrows over the cards, selected paths in the accent colour; no decorative motion | same |
| M12 | **Event flash and live pulse** | a click on a timeline dot; the "Now" marker | the row fades from the accent tint over 1.6 s; the Now dot dims and returns every 2 s | off |
| M13 | **Sign-in and hub** | sign-in page and workspace hub | globe turning, relay feed, rings growing with each typed digit, card scan line, the 0.42 s shake on a wrong code, the takeover overlay (name unscrambling in about 0.95 s, stages stepping, vault door opening) | decoration stills, takeover shortened |

Other small motions: the theme thumb (0.45 s), the phone sidebar slide (0.2 s), the host load bars
(0.4 s), the border and colour fades on buttons (150 ms).

## 6. Arthur

### 6.1 Placement and captions

Arthur is the owner's alpaca avatar. He stands in the sidebar under the brand block, 128 px wide,
centred. He is drawn from 192 by 208 px frames (a gaze atlas and 32 pose strips, one per action), played by the
package's own player. If his files fail to load, he is hidden and the page works without him. His
caption sits under him in 12 px muted text and says why he does what he does. The slot has no
tooltip of its own: the caption already says it.

### 6.2 Looking at the cursor

Whenever the pointer moves (a mouse or pen; touch is ignored) Arthur turns toward it, **also while
he works**, because gaze exists only in his idle pose, so he leaves his work pose for the idle one
while the pointer is moving. When the pointer has been still for **2.5 s** he goes back to his work
pose. He keeps looking at the pointer as it moves even over the rest of the page.

### 6.3 Click tricks

He is a button. Each click (or Enter or Space) plays the next trick once, immediately, cutting
short a pace or a reaction: jump (celebrate), wave (greeting), pencil (thinking), book (reading),
magnifying glass (searching), then round again. The caption keeps its normal text.

### 6.4 The loop: one pose at a time, by priority

The first row that is true wins. The first package's order (problem, waiting, searching, reading,
thinking, typing, idle) is kept; the second package's poses sit between those rows.

| Priority | Pose | When | Caption |
|---|---|---|---|
| 1 | problem | the live connection has been lost for **30 s or more**: the connection row has read "Reconnecting" or "Offline" that long (a shorter break is a reconnect: a proxy ends a stream after 600 s and the browser retries in 3 s; "Stale", "Preparing" and "Live - capture gaps" are a reachable server and do not count) | Offline |
| 2 | mimic_typing | keys are being typed in the Ctrl K search, until 600 ms after the last key | Typing along with you |
| 3 | telescope | the Roadmap page is open (task view or mission view) | Looking over the roadmap |
| 4 | waiting | the owner owes something: an open batch item, a presented brief with no signature, or an open order | Waiting on you: 1 order, 2 questions, 1 brief to sign |
| 5 | juggling | 8 or more tasks in flight and jobs running together | 8 tasks in flight / 2 jobs running |
| 6 | headphones | a research run has been active for more than 30 minutes | 1 research run active over 30 min |
| 7 | drinking_coffee | a job has been running for more than 1 hour | 1 job running over an hour |
| 8 | searching | a research run is starting, running or stopping, or a job runs | 1 research run active / 2 jobs running |
| 9 | reading | a proof is being written for a task in flight, or a log review runs (a review assigned under a live lease, or an open research verification; review events count once a profile names its review event kinds) | Writing a proof; Reviewing a log |
| 10 | thinking | a design-phase task is in flight, or a self-check runs | Designing: 1 task; Self-check running |
| 11 | checklist | a verify-phase task is in flight | Verifying: 1 task |
| 12 | typing | tasks are in flight (in progress with a live lease) | 2 tasks in flight |
| 13 | sleeping | nothing is in flight and nothing has moved in the record for 30 minutes, or it is night on the orb's clock | Asleep: quiet for 42 min / Asleep: night, nothing in flight |
| 14 | idle | none of the above | Idle |

The two page poses (rows 2 and 3) come before waiting because they answer what the person is doing
right now and last only while they type or look at the Roadmap; a new owner wait still makes Arthur
raise a hand (section 6.5). juggling, checklist and sleeping play their entry once and hold the last
pose; the others loop. Every record value comes from the `arthur` block of the overview, never from
free text.

### 6.5 Reactions (one-shot)

A reaction plays once when a counter or the newest event id of a kind **grows between two
overview reads**; the first read after load never triggers one. Several at once queue and play in
turn, and the caption names the cause for the length of the reaction.

| Reaction | Triggers | Caption |
|---|---|---|
| raising_hand | a new owner wait appears (a batch item, a brief to sign or an order); the waiting loop stays while it is owed | A new question for you |
| shield_up | the research guard denies a call, a gate reports BLOCKED, or the git guard denies a command (the hook never opens the record; each deny appends a line to `.alpaca/git-guard/denials.jsonl` and the overview reports that file's size as `git_blocked`) | A guard or a gate blocked a step |
| merging_papers, then celebrate | a merge lands on main (the head of the local main branch changes and has more than one parent) | Merged to main |
| celebrate | a task reaches done; an op closes; a gate passes (a PASS run or self-check row) | A task is done / An op closed / A gate passed |
| dance | every task of an op becomes done | Every task of op-x is done |
| stamping | a proof is sealed | A proof was sealed |
| packing_box | a release build passes | A release candidate was built |
| problem | a gate FAILs (a FAIL run row, a FAIL self-check or a closed phase door); a review says NO-GO; the research engine's test gate FAILs | A gate failed / A review said NO-GO / Tests failed |
| greeting | a new session joins; also once on page load | A new session joined |

### 6.6 Pacing

When Arthur is idle, nothing is playing, the pointer has not moved and he has not paced for **3
minutes**, he paces across the slot: three legs (right, all the way left, back to the middle) of
2.12 s, 4.24 s and 2.12 s, playing his move poses while the stage slides. He also paces once on
every page switch (not while offline); a hash change that stays on the same page (a filter, a
search, a date, the Roadmap or Mission tab) is no switch. A click ends a pace. The slot is checked every 15 s.

### 6.7 Assets and cost

The artwork is the owner's own and ships in every project with no license line. The first package's
11 strips are copied byte for byte. Its gaze atlas is cut to the full-width rows of the 16 gaze
frames the avatar draws (1536 x 416, decoded 2.5 MB instead of 14 MB) and shipped as a lossless WebP;
Chrome draws about 18,700 edge pixels of those frames one level off the PNG's. The owner accepted this
deviation from the byte-for-byte rule on 2026-10-12 to cut the first visit's download.
`alpaca/web/vendor/arthur/arthur-avatar.js` carries one patch: it loads the idle strip first and every other image the first time a draw needs it (`stripsManifest` in `alpaca/web/arthur.js` handles the cut atlas).
The second package's 21 new actions are shipped as
one 192 x 208 strip per action built from the package's own normalized frames, pixels unchanged.
The package's `assets/avatar-manifest.js` ships byte for byte; it still names the full-size sheets,
so the Studio reads it through `stripsManifest` (`alpaca/web/arthur.js`), which points each new
action at its strip and moves the gaze frames up by the cut (`setup/vendor_arthur.py` rebuilds the
strips and the atlas; `alpaca/web/vendor/README.md` records the package version and hash).
`alpaca/web/vendor/arthur/` is about 8.3 MB. Arthur stays on his idle strip until the first overview
arrives and loads each other pose's strip the first time it plays, so a first visit no longer fetches
all of them. Arthur's files are linked with a content version (`?v=`) and kept by the browser for a
day; a changed file has a new URL, and any other request revalidates by its ETag (answers 304).

### 6.8 Reduced motion

With reduced motion Arthur shows the first frame of the current pose: the pose still changes with
the state, but nothing animates and he does not follow the pointer. Pacing plays the move poses
in place with no sliding.

### 6.9 Package 2 actions

The owner's second package adds 21 actions. 20 are wired; their loops are in section 6.4, their
record reactions in section 6.5, and the rest are page reactions:

| Action | Plays when | Kind |
|---|---|---|
| spit | Arthur is clicked 10 times within 2 s (the clicks before that play the five tricks) | page reaction |
| sunglasses | the theme is switched to light | page reaction, once |
| nightcap | the theme is switched to dark | page reaction, once |
| yawn_stretch | the tab becomes visible again after 5 minutes or more hidden | page reaction |
| peekaboo | the sidebar scrolls (its scroll position moves; a wheel turn on a sidebar that fits the window does not count) after Arthur has been at rest for 60 s | page reaction |
| counting_coins | the token board on the Sessions page shows a fresh cost spike (a runaway cell) | page reaction |
| party_hat | today is listed in the optional `studio.party_days` list of `project.yaml` (empty by default; the owner adds dates there) | once per day |

Arthur's resting pose stays plain idle, so cursor gaze keeps working; the theme poses play once on
the switch instead of becoming his resting look.

**detective is not wired.** Its roster trigger is "a debug or investigation task is in flight", and
no record field marks a task as one (the phases are requirement, design, build, verify and
release). A profile or a task field that marks investigation work would let it be wired later.

## 7. Live updates

### 7.1 The push channel

The browser opens `GET /events`, a server-sent stream (`EventSource`). The server keeps it open,
sends a comment ping every 15 s so proxies keep it, and tells the browser to retry in 3 s if it
drops. The host hub ends a stream every 600 s; the browser reconnects by itself. The stream carries
a `rev` event whose data is one small JSON object with a revision per surface, `{"data", "lib",
"board", "pricing"}`. The first `rev` after connecting is the baseline and changes nothing. A `job`
event is also sent; the Studio does not listen for it (Live logs poll on their own).

The server's watcher looks for change every 0.7 s. The board is rebuilt at most every 15 s and the
analytics fold at most every 60 s, so a record change can reach the page up to 15 s later (sessions
up to a minute). The overview itself is shared by all viewers for up to 15 s while the record head
stays put.

### 7.2 Which part refreshes

When a `rev` differs from the last, the browser turns the revision keys into flags:

| Surface | Marks stale |
|---|---|
| board | overview, runs |
| data | sessions, analytics |
| pricing | sessions, analytics |
| lib | library |

An unknown surface, or an unreadable value, marks all five. Each page names the flags it reads:
Roadmap, Research, In-flight, Version control, Work, Activity and Messages read `overview`;
Cockpit reads `overview`, `runs`, `sessions`; Overview and Runs read `overview`, `runs`; Library
reads `library`; Sessions reads `sessions`, `analytics` (a session's Work & reports tab also reads
`overview`); Live logs read none. Only a page that reads a stale flag refreshes, so a moving board
no longer rebuilds Sessions.

### 7.3 Without a full repaint

An automatic refresh does not reset the page. On In-flight, Version control, Sessions and the lower
Cockpit panels the new markup is merged into the old (M6): scroll, focus, open folds, typed text
and chosen filters stay. The other pages are built again with the scroll position put back, and the
Roadmap and Mission map restore their fold, selection and filter state from the browser. A refresh
waits until the reader is idle: the tab is visible, nothing in the main area or navigation has
focus, no dialog is open, no field is being typed in, no log is open, and the Cockpit is not paused.
It runs 1.5 s after the change so a burst becomes one refresh.

### 7.4 Generation counters

A flag is cleared by the fetch that read the data, but only if no newer revision marked it while the
fetch ran. A change that lands mid-fetch is never swallowed; the page refreshes again.

### 7.5 The "New activity available" notice

When a revision touches a cache the open page reads, the clock in the topbar is replaced by "New
activity available" and the small orb beside it listens. The page refreshes by itself when the reader
is idle. If the reader is busy (for example in a dialog or reading a conversation), the notice stays
until they finish or press the pulse orb. If a quiet refresh fails, the page stays as it is, the
connection row says "Stale", the flags are set again and the notice shows; the next change or idle
moment retries.

### 7.6 The 15 s timer

Independent of the push channel, seven pages re-read the record every 15 s: Overview, Cockpit,
In-flight, Version control, Research, Roadmap and Activity. The orb beside the clock shows while it
runs, and the call is skipped while the reader is busy as in 7.3. Other pages refresh on pushed
changes only. Own timers: the host load panel every 3 s, Live logs about every 2 s for log bytes and
10 s for the rest, a followed run log every 2.5 s, the workspace hub's tiles every 15 s, the pulse
orb's repaint every 30 s, the clock every 15 s.

### 7.7 The connection row and Arthur's offline rule

The sidebar's connection row combines three observations. The stream: "Reconnecting" while it is
down. The health check: `/hub/vitals.json` every 10 s (about 1 KB; `/health.json` keeps the full
capture lists) gives "Live", "Live - capture gaps", "Stale", "Preparing" or "Page error", with
details in the tooltip, and "Offline" if the call fails. The same answer carries the health score
the sidebar's health bar shows (section 2.2). The page
read: "Stale" when a quiet refresh fails. Any problem turns the row amber and its orb to
"connecting". Arthur calls the connection lost only after 30 s of "Reconnecting" or "Offline". "Stale" neither
starts nor clears that wait: while the server is gone the 15 s refresh writes it between the health check's
"Offline", and a wait that ends on it still counts as lost. The other amber states mean the server still
answers (section 6.4).

### 7.8 The orb as reload

The pulse orb is the manual reload: it throws away the cached overview, runs and sessions, shows the
loader and reads everything again. It is the one way to pick up a change that is not pushed.

## 8. What a project may change, and what the template keeps stable

### 8.1 What a project may change

Everything. The template ships this look as the default and enforces nothing. A project that wants
another look may:

- restyle any part, shell tokens included, with its own stylesheet (the profile's `web()` `css`),
  linked after `theme.css` (every colour, size, radius and space comes from the tokens in section 3,
  so overriding the token values restyles the Studio);
- add pages and cards through its **profile** (`web()` in the domain profile, section 4.0): add
  **pages of its own** (a module that exports `render(ctx)` and may export `bind(ctx)` and `stop()`),
  turn on the Runs & logs and Live logs pages (`pages: ["runs", "live"]`, with their routes), add
  Cockpit **cards** (a module that exports `render(overview, runs)` and may export `bind()`, with an
  optional `css`), all served from the profile's own assets, name its stages (`stage_names`) and
  serve its own data (`routes()`);
- let the file browser read its own text file types (`paths()["text_exts"]`, for example
  `[".proto", ".graphql"]`), added to the generic list;
- replace Arthur's artwork or drop him (the player hides itself if its files are missing).

A project keeps all of this in its profile package and avoids editing the files under `alpaca/web/`.

`alpaca upgrade` refreshes `alpaca/web/` like every other harness path. It lists a file there that the
project changed as modified and replaces it; the older copy stays in the upgrade backup. Changes kept
outside `alpaca/web/` survive an upgrade untouched.

`alpaca/tests/test_studio_new.py` checks both ends: a project made by `alpaca new` opens every page of
the registry that needs no profile on its empty record (no console error, no error view, an empty state
that says what is missing), and an upgrade of a project with a profile page replaces `alpaca/web/` with
the release's bytes and keeps the package, `project.yaml` and the page in the nav.

### 8.2 What the template keeps stable across upgrades

This is not a prohibition. It is the list of things the template will not move, so a project's own
stylesheet, cards and bookmarks keep working after an upgrade.

- **Shell structure.** The sidebar, topbar, main area, footer, inspector, search dialog and toast,
  and their element ids (`sidebar`, `navigation`, `main`, `crumb`, `crumb-project`, `checked-at`,
  `pulse-orb`, `arthur-slot`, `arthur`, `arthur-caption`, `connection`, `inspector`, `command`,
  `toast`), and the order of the topbar groups.
- **Route names.** The page ids `overview`, `roadmap`, `cockpit`, `inflight`, `research`, `work`,
  `runs`, `live`, `activity`, `messages`, `library`, `records`, `sessions`, `vcs`; their address parameters
  named in section 4; the retired addresses (`#mission`, `/cockpit/legacy/`, `/analytics/legacy/`,
  `/board`) that still redirect.
- **Server paths and shapes.** The `/hub/*.json` endpoints named in section 4, `/events`,
  `/health.json`, `/hub/vitals.json`, `/board/data.json`, `/data.json`, and the fields the pages read from the overview,
  including its `arthur` and `profile` blocks.
- **The live update contract.** The `rev` event and its four keys, the five flags and the table that
  maps one to the other, the 15 s timer, the generation rule, the "New activity available" notice
  and the pulse orb as reload (section 7).
- **The slot API.** The profile `web()` keys `pages` (strings and page objects), `cards`, `css`,
  `assets` and `stage_names`; the card module shape `render(overview, runs)` and `bind()`; the page
  module shape `render(ctx)`, `bind(ctx)`, `stop()` and the keys of `ctx` version 1 (section 4.0);
  the overview's `profile.slot_pages` and `profile.styles`; asset names served under
  `/hub/assets/profile/`; the profile `routes()` seam.
- **Token names.** The CSS variable names in section 3 (their values are the default and free to
  change), and the storage keys `alpaca.theme`, `alpaca.login-theme`, `alpaca.hub-theme`,
  `alpaca.roadmap.*`, `alpaca.live.mode` and the Cache API name `alpaca-handoff`. Renaming a key
  would reset every viewer's saved state.
- **The page order and names** of the sidebar (section 2.2), and the animation set M1 to M13 and
  the live update mechanisms as written in sections 5 and 7 (7.1 to 7.8).

A change to the stable list is a change to the template itself: it goes through the same design and
owner review as the look did, and this document is updated in the same change.
