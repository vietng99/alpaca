# Publishing to a host hub

A host hub is one page at a host's root hostname that every workspace on the machine publishes its
own tile to. The hub never fetches anything from a workspace and never uses a workspace's
credentials: each workspace pushes one small JSON file into a shared drop directory, and the hub
reads, checks and shows those files. Each workspace keeps its own cockpit and hostname; the hub
only links a cockpit whose hostname its admin approved. The hub server itself is not part of this
repository. This page covers the publishing side, `alpaca hub`.

## The tile

`<drop>/<user>--<slug>.json`, owned by `<user>` (the hub ignores a file whose owner is not the
user it names), at most 16 KiB, written with mode 0640:

```
{"v": 1, "user": "...", "slug": "...", "name": "...", "what": "...", "href": "https://...",
 "updated": "ISO-8601 with offset",
 "status": {"headline": "...", "progress": {"done": 0, "total": 0, "label": "..."},
            "counts": {"label": 0, "...": "at most 6"}, "next": "...", "level": "...", "live": 0}}
```

The fields come from this project's own projections, never from its record:

- from `data.json` (written by the Stop hook): the first open operation as the headline, rows
  discharged (or tasks done) as progress, counts of open operations and doing, queued and done
  tasks, the next action, the autonomy level and the number of live sessions;
- else from `board.json`: row counts per column.

Caps: name 48, what 120, href 200, headline 200, next 240, level 16, progress label 48, count
label 24. Tile text drops control, format (bidi overrides, zero-width), private-use, unassigned and
blank-looking filler characters, the same rule the hub applies.

## Settings

In `project.yaml`:

```
hub:
  enabled: false               # true: publish at session end, and let `alpaca hub publish` write
  drop: /srv/alpaca-hub/tiles  # the drop directory; $ALPACA_HUB_DROP overrides it
  slug: null                   # default: the project name in lower case, hyphen-joined
  name: null                   # the tile title; default: the project name
  href: null                   # your cockpit URL, e.g. https://name.example.com/
  what: null                   # one line; default "Workspace, published from data.json"
```

`href` must be an https URL naming a DNS hostname, printable ASCII, with no user name or
password, at most 200 characters; it is checked before anything is written.

## Verbs

```
alpaca hub publish [--print] [--drop DIR]
alpaca hub timer
```

- `alpaca hub publish` writes the tile atomically (a hidden temp file in the drop, then a rename)
  and prints `published <path>`. It refuses (exit 2) while `hub.enabled` is false. `--print`
  writes the tile to stdout and nothing to the drop, whatever `hub.enabled` says. The drop is
  `--drop`, else `$ALPACA_HUB_DROP`, else `hub.drop`, else `/srv/alpaca-hub/tiles`.
- At session end the SessionEnd hook publishes when `hub.enabled` is true. A publish error is
  recorded as a `hub-publish-failed` event and never breaks the hook; with `enabled: false` the
  hook does nothing for the hub.
- `alpaca hub timer` prints a systemd user service and timer (`alpaca-hub-publish-<slug>`) that run
  `bin/alpaca hub publish` every 60 s. It installs nothing. To install, as yourself: save the two
  parts as the files named in the output, then
  `systemctl --user daemon-reload && systemctl --user enable --now alpaca-hub-publish-<slug>.timer`.
  The hub marks a tile stale after a set age, so a timer keeps it fresh between sessions.

## Safety

- A projection that is a symlink or not a regular file (a FIFO, a device, a directory) is refused,
  never followed, and never opened for a blocking read.
- The publisher opens no network connection and reads nothing but `data.json` and `board.json`.
- The tile is written through an exclusive, non-following temp file in the drop; a tile over
  16 KiB is refused before anything is written.
