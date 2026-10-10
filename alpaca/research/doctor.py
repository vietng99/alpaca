"""Doctor findings of the research engine (architecture.md section 3.9): each pack's declared tools
resolved by the rule a run uses, a working jail, and the reproducer store neither tracked nor left
unignored by git.

alpaca/doctor.py calls checks(root, None) from RECORD_FREE_MODULES before its record check, so a
fresh checkout with no record (.alpaca/alpaca.db) is checked too. The rows:

- research-tools: an error row per pack and tool when a tool the pack declares
  (research.fingerprint.tools, the pack's tool declaration) does not resolve on the tool PATH a
  run freezes its tools on (the caller's PATH without the project, the home folder and the other
  forbidden folders), resolves inside a forbidden folder, or is not found on the gate PATH every
  gate runs with. An error row per pack and finding when the run's own validator
  (runbook.research_findings, as `alpaca runbook check` and a run start call it) refuses the
  declaration itself, for example a `tools` that is not a list (FIELD-NOT-LIST); only an absent
  `tools` reads as the default, no tools. An ok row per pack names the tools that resolved and
  where. A warn row names a runbook whose tool declaration cannot be read. One ok row says so when
  the project holds no format 3 runbook. Alpaca never installs a pack's tools: the pack's own
  setup does.
- research-jail: ok when bubblewrap works, else a warn (a run start is refused on that host).
- research-store: ok when contracts/research/ tracks nothing and git ignores it, else a warn.

The jail and store rows appear only when the project holds a format 3 runbook. The checks write
nothing in the project; the jail probe (alpaca/research/jail.py available) makes and removes one
temporary folder under the system temp folder. Every engine module is imported inside a function,
so a module that does not load gives a warn row here instead of hiding the check."""

from __future__ import annotations

import os
import re
import subprocess

#: left out of the listing besides catalog._SKIP: Alpaca's own package, whose test fixtures are
#: runbooks of no project pack
_LEAVE_OUT = ("alpaca/",)
#: the reproducer store, relative to the project root (paths.reproducer_store); git ignores it,
#: so a folder walk drops it by hand
_STORE = "contracts/research"
#: folder names a folder walk never enters, besides every name that starts with "."
_WALK_PRUNE = ("node_modules", "__pycache__")
_TOOL_PATH = ("the tool PATH (the caller's PATH without the project, the home folder and the other "
              "forbidden folders), the PATH a run freezes its tools on")


def checks(root, conn) -> list[tuple[str, str, str]]:
    """[(name, level, detail)] with level ok, warn or error. conn is not read: alpaca/doctor.py
    passes None, and the parameter keeps the shape every check module shares."""
    root = os.path.realpath(root)
    rows: list = []
    fmt3 = 0
    try:
        tool_rows, fmt3 = _tool_rows(root)
        rows += tool_rows
    except Exception as exc:
        rows.append(("research-tools", "warn", "tool check skipped: %s: %s" % (type(exc).__name__, exc)))
    if fmt3:
        for name, step in (("research-jail", _jail_row), ("research-store", lambda: _store_row(root))):
            try:
                rows.append(step())
            except Exception as exc:
                rows.append((name, "warn", "check skipped: %s: %s" % (type(exc).__name__, exc)))
    return rows


# ------------------------------------------------------------------------------ the listing
def _listing(root):
    """(relative paths, source): the *.yaml and *.yml files to read, sorted. Listed by `git
    ls-files` as catalog._tracked lists them when git answers (source "git"); by a folder walk with
    the same skips only when git cannot answer (source "walk"), so a copy with no git metadata is
    still checked. catalog._SKIP and alpaca/ are left out of both."""
    from alpaca.research import catalog
    skip = tuple(catalog._SKIP) + _LEAVE_OUT
    try:
        got = subprocess.run(["git", "-C", root, "ls-files", "-z", "--", "*.yaml", "*.yml"],
                             capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        got = None
    if got is not None and got.returncode == 0:
        rels = set()
        for name in got.stdout.split(b"\0"):
            rel = name.decode("utf-8", "replace")
            if rel and not rel.startswith(skip) and os.path.isfile(os.path.join(root, rel)):
                rels.add(rel)
        return sorted(rels), "git"
    return _walk(root, skip + (_STORE + "/",)), "walk"


def _walk(root, skip):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = os.path.relpath(dirpath, root).replace(os.sep, "/")
        prefix = "" if here == "." else here + "/"
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d not in _WALK_PRUNE
                             and not (prefix + d + "/").startswith(skip))
        for name in filenames:
            # a regular file only, as the git listing tests it: a FIFO named *.yaml would block
            # the read of its head
            if (name.endswith((".yaml", ".yml")) and not (prefix + name).startswith(skip)
                    and os.path.isfile(os.path.join(dirpath, name))):
                out.append(prefix + name)
    return sorted(out)


# ------------------------------------------------------------------------- the declarations
def _declarations(root, rels):
    """([(rel, pack, block, tools, refused)], warn rows, count of format 3 runbooks) for the listed
    files. A file with no `research:` in its head, or that is not a format 3 runbook (a pack.yaml
    defaults file, a format 1 or 2 runbook), is skipped. Only a block with no `fingerprint` or no
    `fingerprint.tools` reads as [] (the default for a pack that declares nothing). `refused` holds
    the run validator's findings on the declaration (_refused): a `tools` that is not a list is one
    of them, read as [] for the resolution and never as a pack that declares nothing."""
    from alpaca import runbook
    from alpaca.research import catalog
    found, rows, fmt3 = [], [], 0
    for rel in rels:
        path = os.path.join(root, rel)
        if "research:" not in catalog._head(path):
            continue
        data, report = runbook.load(path)
        if data is None:
            rows.append(_unreadable(rel, report.errors))
            continue
        if runbook.research_block(data) is None:
            continue
        fmt3 += 1
        base = os.path.dirname(path)
        block, _layers, report = runbook.expand_research(data, base)
        pack = _pack_name(runbook, data, base, rel)
        if report.errors or not isinstance(block, dict):
            refused = _refused(report.errors)
            if refused:
                # a declaration the layers cannot even be merged with: the run refuses it too
                found.append((rel, pack, None, [], refused))
            others = [e for e in report.errors if not _at_declaration(e)]
            if others or not refused:
                rows.append(_unreadable(rel, others))
            continue
        try:
            errors, _warnings = runbook.research_findings(data, block, base, check_files=False)
        except Exception as exc:
            rows.append(("research-tools", "warn", "cannot check the tool declaration of %s: %s: %s"
                         % (rel, type(exc).__name__, exc)))
            continue
        node = block.get("fingerprint")
        tools = node.get("tools") if isinstance(node, dict) else None
        found.append((rel, pack, block, tools if isinstance(tools, list) else [], _refused(errors)))
    return found, rows, fmt3


def _pack_name(runbook, data, base, rel):
    """The defaults file's `pack`, else the runbook's id, else its path."""
    try:
        pack = runbook._research_pack(data, base).get("pack")
    except Exception:
        pack = None
    if isinstance(pack, str) and pack:
        return pack
    rid = data.get("id")
    return rid if isinstance(rid, str) and rid else rel


#: a finding on the tool declaration: research.fingerprint in the merged block, or
#: `<defaults file>:research.fingerprint` in the layer that wrote it (runbook._research_layers)
_DECLARATION = re.compile(r"(?:^|:)research\.fingerprint(?:$|[.\[])")


def _at_declaration(error):
    return bool(_DECLARATION.search(str(error.get("where") or "")))


def _refused(errors):
    """[(code, where, message)] of the validator findings that refuse the declaration itself, in the
    order found. Left out: FIELD-MISSING (an absent `fingerprint` or `tools` is the default, a pack
    that declares nothing) and TOOL-INVALID, which _resolve reports per name, so a name is blamed
    once."""
    out = []
    for e in errors:
        where = str(e.get("where") or "")
        code = e.get("code")
        if not _at_declaration(e):
            continue
        if code in ("FIELD-MISSING", "TOOL-INVALID"):
            continue
        item = (str(code), where, str(e.get("message") or ""))
        if item not in out:
            out.append(item)
    return out


def _unreadable(rel, errors):
    first = errors[0] if errors else {"code": "BLOCK-INVALID", "message": "no research block"}
    return ("research-tools", "warn", "cannot read the tool declaration of %s: %s %s"
            % (rel, first.get("code"), first.get("message")))


# ------------------------------------------------------------------------------- resolution
def _bare(name):
    from alpaca import runbook
    return isinstance(name, str) and bool(runbook.TOOL.match(name)) and name not in (".", "..")


def _resolve(root, block, names):
    """[(name, realpath or None, problem or None)] for one runbook's declared tools, by the rule a
    run uses: first the tool PATH contract freeze resolves on (contract._forbidden, _tool_dirs,
    _resolve_tool, as contract.preview calls them), a realpath inside a forbidden folder refused as
    check_overlap refuses it; then the gate PATH built once from every resolved realpath of the
    runbook (procs.gate_path), on which fingerprint.resolve_tools must find each name, as
    fingerprint.take does at every gate."""
    from alpaca.research import contract, fingerprint, procs
    repo = root
    node = block.get("repo")
    declared = node.get("path") if isinstance(node, dict) else None
    if isinstance(declared, str) and declared:
        repo = os.path.realpath(declared if os.path.isabs(declared) else os.path.join(root, declared))
    forbidden = contract._forbidden(root, repo, contract._rules(block, "paths", "hidden"),
                                    contract._rules(block, "holdout", "paths"))
    dirs = contract._tool_dirs(forbidden)
    first = []
    for name in names:
        if not _bare(name):
            first.append((str(name), None, "TOOL-INVALID: not a bare command name"))
            continue
        real = contract._resolve_tool(name, dirs)
        if real is None:
            first.append((name, None, "not found on " + _TOOL_PATH))
            continue
        bad = next((f for f in forbidden if contract._within(real, f)), None)
        first.append((name, real, None if bad is None else
                      "resolves to %s, inside forbidden path %s" % (real, bad)))
    gate = procs.gate_path(tuple(real for _n, real, _p in first if real is not None))
    valid = tuple(name for name, _r, problem in first if problem is None)
    pins = fingerprint.resolve_tools(valid, gate) if valid else {}
    out = []
    for name, real, problem in first:
        pin = pins.get(name) if problem is None else None
        if pin is not None and pin.error:
            problem = ("found at %s on the tool PATH, but on the gate PATH %s that every gate runs "
                       "with it gives: %s; the first gate of a run would fail"
                       % (real, gate, pin.error))
        out.append((name, real, problem))
    return out


def _tool_rows(root):
    """(rows, number of format 3 runbooks found)."""
    rels, source = _listing(root)
    declared, rows, fmt3 = _declarations(root, rels)
    packs: dict = {}
    for rel, pack, block, tools, refused in declared:
        entry = packs.setdefault(pack, {"rels": [], "ok": {}, "bad": {}, "refused": {},
                                        "declares": False})
        entry["rels"].append(rel)
        entry["declares"] = entry["declares"] or bool(tools) or bool(refused)
        for code, where, message in refused:
            entry["refused"].setdefault((code, where), [message, []])[1].append(rel)
        if not tools:
            continue
        for name, real, problem in _resolve(root, block, tools):
            if problem:
                entry["bad"].setdefault(name, [problem, []])[1].append(rel)
            else:
                entry["ok"].setdefault(name, [real, []])[1].append(rel)
    for pack in sorted(packs):
        entry = packs[pack]
        for (code, at), (message, where) in entry["refused"].items():
            rows.append(("research-tools", "error",
                         "pack %s declares its tools in a form a run refuses (%s): %s %s: %s. A run "
                         "start refuses it (CONTRACT-CHECK-FAILED); write `tools` as a list of bare "
                         "command names, then run doctor again"
                         % (pack, ", ".join(where), code, at, message)))
        for name, (problem, where) in entry["bad"].items():
            rows.append(("research-tools", "error",
                         "pack %s declares tool %s (%s): %s. Alpaca never installs a pack's tools: "
                         "install %s as the pack's own setup says, then run doctor again"
                         % (pack, name, ", ".join(where), problem, name)))
        good = [(name, real) for name, (real, _w) in entry["ok"].items() if name not in entry["bad"]]
        if good:
            rows.append(("research-tools", "ok", "pack %s: %s (%s)" % (
                pack, ", ".join("%s at %s" % pair for pair in good), ", ".join(entry["rels"]))))
        elif not entry["declares"]:
            rows.append(("research-tools", "ok", "pack %s declares no tools (%s)"
                         % (pack, ", ".join(entry["rels"]))))
    if not fmt3 and not rows:
        rows.append(("research-tools", "ok", "no format 3 runbook in this project (listed by %s)"
                     % ("git" if source == "git" else "a folder walk")))
    return rows, fmt3


# ------------------------------------------------------------------------ jail and the store
def _flat(text, limit=300):
    return " ".join(str(text).split())[:limit]


def _jail_row():
    from alpaca.research import jail
    ok, why = jail.available()
    if ok:
        return ("research-jail", "ok", "bubblewrap works: %s" % _flat(why))
    return ("research-jail", "warn", "a run start is refused (RUN-JAIL-UNAVAILABLE): %s; install "
            "bubblewrap, allow unprivileged user namespaces, or start the run outside another "
            "sandbox" % _flat(why))


def _store_row(root):
    """RUN-STORE-TRACKED's own predicate (runner._git_tracked), then whether git ignores the store."""
    from alpaca.research import errors, runner
    try:
        names = runner._git_tracked(root, _STORE)
    except errors.ResearchRefusal as exc:
        return ("research-store", "warn", _flat(exc, 600))
    if names is None:
        return ("research-store", "ok", "not a git work tree: nothing can be tracked")
    if names:
        return ("research-store", "warn", "%d path(s) of the reproducer store %s/ are tracked, for "
                "example %s; a run start refuses RUN-STORE-TRACKED" % (len(names), _STORE, names[0]))
    try:
        done = subprocess.run(["git", "-C", root, "check-ignore", "-q", _STORE + "/x"],
                              capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return ("research-store", "warn", "git check-ignore did not run: %s" % exc)
    if done.returncode == 0:
        return ("research-store", "ok", "%s/ tracks nothing and is ignored by git" % _STORE)
    if done.returncode == 1:
        return ("research-store", "warn", "%s/ is not ignored by git: add /%s/ to .gitignore (FR-006)"
                % (_STORE, _STORE))
    return ("research-store", "warn", "git check-ignore exited %d: %s"
            % (done.returncode, _flat(done.stderr.decode("utf-8", "replace"))))
