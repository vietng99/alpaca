"""alpaca start: the one entry point from raw notes to intake.

It picks the spec tool, prepares the project for it, and prints the steps that take the notes to a
spec, the spec to a runbook, and the runbook to intake. The skill `.claude/skills/alpaca-from-notes/`
walks a person through those steps in Claude Code; this verb is its small helper and works the
same from a shell.

The pick (the person can override it with --kit):

  * a new thing, in a project with no spec yet: spec-kit (a raw idea to a first spec, with the
    clarify questions);
  * a change to something that already has specs: OpenSpec (living specs and change deltas);
  * a project that already records OpenSpec keeps OpenSpec.

A project uses one kit at a time. The first change to a spec-kit project moves it to OpenSpec:
`--prepare` installs OpenSpec and writes each spec-kit spec as a living OpenSpec spec, one
requirement per success criterion and functional requirement. It moves once: when `moved_from`
is already in the `spec:` block, a later `--prepare` leaves the moved specs and the block alone.
Two feature folders that map to one capability name are refused. Each scenario is named by its
spec-kit id (`#### Scenario: SC-002`) and holds the criterion text, so the runbook's `covers`
entries still match and `alpaca intake` keeps every row and its verdicts across the move. The
spec-kit files stay as they are, as history.

    alpaca start <notes> [--kit spec-kit|openspec] [--prepare] [--json]

Without --prepare the verb only reads. With it, it installs the kit (offline, from the vendored
copies), writes the moved specs (never over an existing file), and records one `start` event.
docs/intake.md is the reference.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

from alpaca import db, util

INSTRUMENT = "alpaca-start"
EVENT = "start"
PASS, FAIL, BLOCKED, USAGE = 0, 1, 2, 64
KITS = ("spec-kit", "openspec")


class StartError(Exception):
    def __init__(self, message, code=FAIL):
        super().__init__(message)
        self.code = code


def _rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, "/")


def speckit_specs(root):
    """spec-kit feature specs of the project: specs/<feature>/spec.md, project-relative."""
    return sorted(_rel(root, p) for p in glob.glob(os.path.join(root, "specs", "*", "spec.md")))


def openspec_specs(root):
    """Living OpenSpec specs: openspec/specs/<capability>/spec.md, project-relative."""
    out = []
    base = os.path.join(root, "openspec", "specs")
    for dirpath, dirs, names in os.walk(base):
        dirs.sort()
        if "spec.md" in names:
            out.append(_rel(root, os.path.join(dirpath, "spec.md")))
    return sorted(out)


def state(root):
    from alpaca import spec_kits
    detected = spec_kits.detect(root)
    return {"recorded": spec_kits.recorded(root).get("kit"),
            "installed": [k for k in KITS if detected.get(k)],
            "speckit_specs": speckit_specs(root), "openspec_specs": openspec_specs(root)}


def choose(root, override=None):
    """(kit, mode, reason). mode is "new" (no spec yet) or "change" (specs exist)."""
    st = state(root)
    has_specs = bool(st["speckit_specs"] or st["openspec_specs"])
    mode = "change" if has_specs else "new"
    if override:
        if override not in KITS:
            raise StartError("unknown kit %r; use spec-kit or openspec" % override, code=USAGE)
        return override, mode, "chosen with --kit"
    if st["recorded"] == "openspec" or st["openspec_specs"]:
        return "openspec", mode, ("the project uses OpenSpec; a %s goes in as an OpenSpec change"
                                  % ("change" if has_specs else "new capability"))
    if st["speckit_specs"]:
        return "openspec", "change", ("the project already has a spec (%s), so this is a change: "
                                      "OpenSpec keeps the living spec and the change as a delta"
                                      % ", ".join(st["speckit_specs"]))
    return "spec-kit", "new", "no spec yet, so this is a new thing: spec-kit takes a raw idea to a first spec"


# ------------------------------------------------------------------------------ the move
def capability_name(feature_dir):
    name = re.sub(r"^\d+[-_]", "", os.path.basename(feature_dir.rstrip("/"))) or os.path.basename(feature_dir)
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "spec"


def moved_spec_text(spec_path, rel_path):
    """A living OpenSpec spec that says what the spec-kit spec at `spec_path` says: one
    requirement per SC-nnn and FR-nnn, each with one scenario named by the id and holding the
    item's text."""
    from alpaca import runbook
    spec = runbook.parse_spec(spec_path)
    if spec["format"] != "spec-kit":
        raise StartError("%s is not a spec-kit spec" % rel_path)
    lines = runbook._read_lines(spec_path)
    title = next((re.sub(r"^#\s*(Feature Specification:\s*)?", "", l).strip()
                  for l in lines if l.startswith("# ")), capability_name(os.path.dirname(spec_path)))
    out = ["# %s Specification" % capability_name(os.path.dirname(spec_path)), "",
           "## Purpose", "",
           "%s. Moved from the spec-kit spec %s by alpaca start on %s. Each requirement is one "
           "success criterion (SC-nnn) or functional requirement (FR-nnn) of that spec, and its "
           "scenario keeps the id, so the rows alpaca intake made from the spec-kit spec keep their "
           "keys." % (title.rstrip("."), rel_path, util.now_iso()[:10]), "",
           "## Requirements", ""]
    order = sorted(spec["items"], key=lambda i: (i["kind"] != "SC", int(i["id"].split("-")[1])))
    for item in order:
        what = "success criterion" if item["kind"] == "SC" else "functional requirement"
        text = " ".join(str(item["text"]).split()) or item["id"]
        out += ["### Requirement: %s" % item["id"], "",
                "The system SHALL meet %s %s." % (what, item["id"]), "",
                "#### Scenario: %s" % item["id"], "", text, ""]
    return "\n".join(out)


def _refuse_collisions(root):
    """Refuse (BLOCKED) when two or more feature folders map to one capability name, since the
    move would write only the first of them."""
    caps = {}
    for rel in speckit_specs(root):
        caps.setdefault(capability_name(os.path.dirname(rel)), []).append(rel)
    clash = {cap: rels for cap, rels in caps.items() if len(rels) > 1}
    if clash:
        raise StartError("two spec-kit features map to one OpenSpec capability: %s. Rename a feature "
                         "folder so each name after the number is its own, then run --prepare again"
                         % "; ".join("%s from %s" % (cap, ", ".join(rels)) for cap, rels in sorted(clash.items())),
                         code=BLOCKED)


def _living_target(root, rel):
    return os.path.join(root, "openspec", "specs", capability_name(os.path.dirname(rel)), "spec.md")


def move_to_openspec(root, only=None):
    """Write every spec-kit spec (or those in `only`) as a living OpenSpec spec (never over an
    existing file). Returns [(spec-kit spec, OpenSpec spec, written?)]. Two feature folders that
    map to one capability name are refused before anything is written."""
    _refuse_collisions(root)
    done = []
    for rel in speckit_specs(root):
        if only is not None and rel not in only:
            continue
        cap = capability_name(os.path.dirname(rel))
        target = os.path.join(root, "openspec", "specs", cap, "spec.md")
        text = moved_spec_text(os.path.join(root, rel), rel)
        wrote = not os.path.exists(target)
        if wrote:
            util.write_text(target, text + "\n")
        done.append((rel, _rel(root, target), wrote))
    return done


def prepare(root, kit, mode):
    """Install the kit when it is missing; on a move from spec-kit, write the living specs.
    Returns the list of things done."""
    from alpaca import spec_kits
    st = state(root)
    done = []
    moved_from = spec_kits.recorded(root).get("moved_from")
    moved_from = moved_from if isinstance(moved_from, dict) else None
    if kit == "openspec" and st["speckit_specs"] and not moved_from:
        _refuse_collisions(root)             # before the install, not halfway through
    if kit not in st["installed"] or st["recorded"] != kit:
        other = [k for k in KITS if k != kit][0]
        force = other in st["installed"] or st["recorded"] == other
        if force and kit == "spec-kit":
            raise StartError("the project already uses OpenSpec; a project moves from spec-kit to "
                             "OpenSpec, not back. Rerun with --kit openspec", code=BLOCKED)
        try:
            spec_kits.init(root, kit, force=force)
        except spec_kits.KitError as exc:
            raise StartError("alpaca spec init --kit %s: %s" % (kit, exc),
                             code=BLOCKED if exc.blocked else FAIL)
        done.append("installed %s%s" % (kit, " (the project moves from spec-kit)" if force else ""))
    if kit == "openspec" and st["speckit_specs"] and moved_from:
        before = [str(x) for x in (moved_from.get("specs") or [])]
        added = [rel for rel in speckit_specs(root) if rel not in before]
        if added:
            moved = move_to_openspec(root, only=added)
            for src, dst, wrote in moved:
                done.append("%s %s from %s (a spec-kit feature added after the move)"
                            % ("wrote" if wrote else "kept the existing", dst, src))
            block = dict(spec_kits.recorded(root))
            block["moved_from"] = dict(moved_from, specs=before + [m[0] for m in moved])
            spec_kits.record(root, block)
        for rel in before:
            target = _living_target(root, rel)
            if os.path.isfile(os.path.join(root, rel)) and not os.path.exists(target):
                done.append("the living spec %s moved from %s is missing; it is not written again "
                            "(restore it from git if it was removed by mistake)"
                            % (_rel(root, target), rel))
        done.append("already moved from spec-kit (%s); the spec-kit files stay as history"
                    % ", ".join(before))
    elif kit == "openspec" and st["speckit_specs"]:
        moved = move_to_openspec(root)
        for src, dst, wrote in moved:
            done.append("%s %s from %s" % ("wrote" if wrote else "kept the existing", dst, src))
        block = dict(spec_kits.recorded(root))
        block["moved_from"] = {"kit": "spec-kit", "specs": [m[0] for m in moved]}
        spec_kits.record(root, block)
    return done


# ------------------------------------------------------------------------------ the steps
def _notes(arg):
    """(text, label): the notes from a file, or the argument itself when it names no file."""
    path = arg
    if path == "-":
        return sys.stdin.read(), "stdin"
    if os.path.isfile(path):
        return util.read_text(path), path
    return arg, "inline"


def steps(kit, mode, notes_label, has_op):
    notes = "the notes in %s" % notes_label if notes_label not in ("inline", "stdin") else "the notes"
    out = []
    out.append({"step": "prepare", "do": "install %s and get the project ready" % kit,
                "command": "alpaca start <notes> --kit %s --prepare" % kit})
    if kit == "spec-kit":
        out += [
            {"step": "spec", "do": "write the first spec from %s" % notes,
             "command": "/speckit-specify <the notes>  (in Claude Code; writes specs/<NNN-name>/spec.md)"},
            {"step": "clarify", "do": "answer the open questions until no [NEEDS CLARIFICATION] is left",
             "command": "/speckit-clarify"},
            {"step": "runbook", "do": "write runbook.yaml next to the spec; every SC-nnn gets a check or an owner gate",
             "command": "the runbook forge skill (/alpaca-runbook-forge, .claude/skills/alpaca-runbook-forge/SKILL.md), then alpaca runbook check <runbook> --spec <spec.md>"},
        ]
        spec = "specs/<NNN-name>/spec.md"
    else:
        out += [
            {"step": "spec", "do": "propose the change from %s as an OpenSpec change" % notes,
             "command": "/opsx:propose <the notes>  (in Claude Code; writes openspec/changes/<id>/)"},
            {"step": "validate", "do": "check the change",
             "command": "bin/openspec validate <id> --strict"},
            {"step": "runbook", "do": "update the runbook: cover every new and changed scenario",
             "command": "the runbook forge skill (/alpaca-runbook-forge, .claude/skills/alpaca-runbook-forge/SKILL.md), then alpaca runbook check <runbook> --spec openspec/changes/<id>"},
        ]
        spec = "openspec/changes/<id>"
    if not has_op:
        out.append({"step": "op", "do": "open the op the rows belong to",
                    "command": "alpaca op new \"<one line on the work>\" --done-when \"<the bar>\""})
    out += [
        {"step": "intake", "do": "see the rows, tasks and profile intake would create, then create them",
         "command": "alpaca intake %s <runbook> --dry-run, then without --dry-run" % spec},
    ]
    if kit == "openspec":
        out.append({"step": "archive", "do": "after the change is built and its rows proven, fold it into the living specs",
                    "command": "/opsx:archive <id>, then alpaca intake openspec/specs <runbook> (nothing to change)"})
    return out


def run(root, notes_arg, *, kit=None, do_prepare=False, session="cli"):
    text, label = _notes(notes_arg)
    if not text.strip():
        raise StartError("the notes are empty; pass a file or the text itself", code=USAGE)
    chosen, mode, reason = choose(root, kit)
    from alpaca import paths
    has_op = False
    if os.path.isfile(paths.db_path(root)) or do_prepare:
        conn = db.connect(root)
        has_op = conn.execute("SELECT COUNT(*) FROM ops WHERE status='open' AND id != 'op-0'").fetchone()[0] > 0
    result = {"kit": chosen, "mode": mode, "reason": reason, "notes": label,
              "notes_sha256": util.sha256_hex(text), "state": state(root),
              "steps": steps(chosen, mode, label, has_op), "prepared": []}
    if do_prepare:
        result["prepared"] = prepare(root, chosen, mode)
        db.append_event(conn, session=session, actor=INSTRUMENT, kind=EVENT,
                        data={"kit": chosen, "mode": mode, "reason": reason, "notes": label,
                              "notes_sha256": result["notes_sha256"], "prepared": result["prepared"]})
        result["state"] = state(root)
    return result


def _print(result):
    print("kit: %s (%s)" % (result["kit"], result["mode"]))
    print("why: %s" % result["reason"])
    for line in result["prepared"]:
        print("prepared: %s" % line)
    print("steps:")
    for n, s in enumerate(result["steps"], 1):
        print("  %d. %s: %s" % (n, s["step"], s["do"]))
        print("     %s" % s["command"])


def cmd_start(args):
    from alpaca import cli
    from alpaca.gates import verdict
    if not getattr(args, "notes", None):
        print("usage: alpaca start <notes file or text> [--kit spec-kit|openspec] [--prepare] [--json]",
              file=sys.stderr)
        return USAGE
    notes = args.notes
    if notes != "-" and not os.path.isabs(notes):
        from alpaca import runbook
        candidate = runbook._from_caller(notes)
        if os.path.isfile(candidate):
            notes = candidate
    try:
        result = run(cli._root(), notes, kit=args.kit, do_prepare=args.prepare,
                     session=args.session or "cli")
    except StartError as exc:
        if args.json:
            print(json.dumps({"verdict": verdict.name_of(exc.code) if exc.code != USAGE else "USAGE",
                              "reason": str(exc)}, indent=1))
        else:
            print("GATE %s: %s (%s)" % (INSTRUMENT, verdict.name_of(exc.code) if exc.code != USAGE
                                        else "USAGE", exc))
        return exc.code
    result["verdict"] = "PASS"
    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True))
    else:
        _print(result)
        print(verdict.gate_line(INSTRUMENT, PASS))
    return PASS


def _parser(sub):
    p = sub.add_parser("start", help="from raw notes to intake: pick spec-kit or OpenSpec, prepare the "
                                     "project, and print the steps")
    p.add_argument("notes", nargs="?", help="a file with the raw notes, the notes as text, or - for stdin")
    p.add_argument("--kit", choices=KITS, default=None, help="override the pick")
    p.add_argument("--prepare", action="store_true",
                   help="install the kit, move a spec-kit project to OpenSpec, record the choice")
    p.add_argument("--json", action="store_true")


def _register():
    from alpaca import cli
    cli.command("start")(cmd_start)
    cli.register_parser("start", _parser)


_register()
