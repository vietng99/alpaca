"""Every reader of the Rune2 store or of the record's fact text, run and checked (op-008 t-070 round 3,
class C-E completeness; `.alpaca/rune2-development/execution/t-070/notes-r3.md`).

Round 2 gated the serving doors and listed the rest in `servable.NOT_SERVING` with a reason each.
Check round 2 found one of those reasons false (R2-B1: `record_facts_preview` read the record's fact
text with no gate) and a reader outside the registry (F-2: `alpaca sort`). This module turns the
registry into something a test proves:

- `RUNNERS`: one runner for every Memory method, every `wiki` verb and every other `alpaca` verb that
  reads the store (`sort`, `backup`), exempt or not. `run_every_reader` runs all of them on a store
  and checks every output (return value, printed text, files written, record events appended) for
  the withdrawn words, and for a serving surface also for the withdrawn documents' ids. A runner
  that writes runs on its own copy of the store, and after it the serving surfaces of that copy are
  asked again and no new vault bytes of a withdrawn source may exist.
- `store_readers`: the AST enumeration of every function outside alpaca/tests that reads store rows
  or the record's fact text (it opens the store, connects to its file by hand, or renders record
  events as notes or record facts), with whether its own body consults the serving gate, and the
  calls it makes resolved through each module's imports (`upstream_gated`: a function called only
  from gated code).
- `traced`: which of those functions ran while the runners worked on a store behind the record.
  `alpaca/tests/wiki/test_kill_states.py` fails when a reader is neither gated nor proven that way.
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import io
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

#: Questions with none of the withdrawn words in them, so any withdrawn word in an answer came from
#: the store (the kill test's own questions name the words and strip them from the answer).
PLAIN_QUESTIONS = ("Where is the spare tide gauge kept?",
                   "What does the ledger name as the vault combination?",
                   "Which session decided where the gauge is kept?")


@dataclasses.dataclass(frozen=True)
class Reader:
    """`name` is the registry name (a Memory method, a `wiki` verb, an `alpaca` verb) or a variant of
    one (`<name> <variant>`); `writes` runs it on a copy of the store. Every CLI runner writes: a
    verb's verdict line is a record event (`gates.verdict.emit_verdict`). `behind` is False for a
    variant that brings the store up to the record before it reads (`--refresh`): what it reads
    then ran on a current store, so a trace does not count it as run behind the record."""
    name: str
    writes: bool
    run: object
    behind: bool = True

    @property
    def registered(self) -> str:
        return self.name.split(" ", 1)[0]


# ------------------------------------------------------------------ helpers
def _json(value) -> str:
    return json.dumps(value, default=lambda o: dataclasses.asdict(o) if dataclasses.is_dataclass(o)
                      else str(o), sort_keys=True)


def _appender(root, session):
    from alpaca import db as record

    def append(kind, data):
        conn = record.connect(str(root))
        try:
            return record.append_event(conn, session=session, actor="alpaca-wiki", kind=kind,
                                       data=data)["id"]
        finally:
            conn.close()
    return append


def _gated_call(fn) -> str:
    """`fn()`'s output, or the gate's refusal (`StoreBehindRecord`) as text."""
    from alpaca.wiki import service
    try:
        return _json(fn())
    except service.StoreBehindRecord as held:
        return "unavailable " + _json(held.detail)


@contextlib.contextmanager
def _cwd(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def cli(root, argv) -> str:
    """`alpaca <argv>` in this process from the project's src folder; exit code, stdout and stderr."""
    from alpaca import cli as alpaca_cli
    out, err = io.StringIO(), io.StringIO()
    with _cwd(os.path.join(root, "src")), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        code = alpaca_cli.main(list(argv))
    return "exit %s\n%s\n%s" % (code, out.getvalue(), err.getvalue())


def _files_text(folder) -> str:
    folder = Path(folder)
    if not folder.exists():
        return ""
    if folder.is_file():
        return folder.read_bytes().decode("utf-8", "replace")
    return "".join(p.read_bytes().decode("utf-8", "replace")
                   for p in sorted(folder.rglob("*")) if p.is_file() and p.suffix != ".db")


def record_head(root) -> int:
    conn = sqlite3.connect("file:%s?mode=ro" % os.path.join(root, ".alpaca", "alpaca.db"), uri=True)
    try:
        return int(conn.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0])
    finally:
        conn.close()


def _appended(root, after: int) -> str:
    """The kind and data of every record event after id `after` (a door that copies withdrawn text
    into the record leaks it there)."""
    conn = sqlite3.connect("file:%s?mode=ro" % os.path.join(root, ".alpaca", "alpaca.db"), uri=True)
    try:
        return "".join("%s %s\n" % r for r in conn.execute(
            "SELECT kind, data FROM events WHERE id>? ORDER BY id", (after,)))
    finally:
        conn.close()


def _sessions(root) -> list:
    conn = sqlite3.connect("file:%s?mode=ro" % os.path.join(root, ".alpaca", "alpaca.db"), uri=True)
    try:
        return sorted({r[0] for r in conn.execute(
            "SELECT DISTINCT session FROM events WHERE actor IN ('agent','human') AND session<>''")})
    finally:
        conn.close()


def _proposal(root, ctx) -> dict:
    sys.path.insert(0, os.path.dirname(__file__))
    from native_helpers import proposal
    return proposal(root, ctx["fact"], "blocked on the review", subject="t-1", obj="review")


def _pier_note(scratch) -> str:
    path = Path(scratch, "pier-crane.md")
    path.write_text("The pier crane was painted blue in spring.\n", encoding="utf-8")
    return str(path)


# ------------------------------------------------------------------ the Memory methods
def _m_status(root, ctx, scratch):
    from alpaca.wiki import service
    return _json(service.status(root))


def _m_query(root, ctx, scratch):
    from alpaca.wiki import service
    return "".join(_json({k: v for k, v in service.query(root, q).items() if k != "question"})
                   for q in PLAIN_QUESTIONS)


def _m_context(root, ctx, scratch):
    from alpaca.wiki import service
    return "".join(service.context(root, q, max_chars=16000).replace(q, "")
                   for q in PLAIN_QUESTIONS) + service.context(
        root, PLAIN_QUESTIONS[0], max_chars=16000, briefing_task="t-1").replace(PLAIN_QUESTIONS[0], "")


def _m_briefing(root, ctx, scratch):
    from alpaca.wiki import service
    return "".join(_json(service.briefing(root, **{kind: ident}, max_chars=16000).to_dict())
                   for kind, ident in (("task", "t-1"), ("op", "op-8")))


def _m_ingest(root, ctx, scratch):
    from alpaca.wiki import service
    return _gated_call(lambda: service.ingest(root, _pier_note(scratch), doc_id="wiki/pier-crane.md",
                                              session="s-ingest"))


def _m_record_facts(root, ctx, scratch):
    from alpaca.wiki import service
    return _gated_call(lambda: [dataclasses.asdict(r) for r in service.record_facts_preview(
        root, from_event=1, through_event=record_head(root))])


def _m_propose(root, ctx, scratch):
    from alpaca.wiki import service
    return _gated_call(lambda: service.propose(root, _proposal(root, ctx), session="s-p",
                                               record=_appender(root, "s-p")).to_dict())


def _m_candidates(root, ctx, scratch):
    from alpaca.wiki import service
    return _gated_call(lambda: [v.to_dict() for v in service.candidates(root)])


def _m_admit(root, ctx, scratch):
    from alpaca.wiki import service
    return _gated_call(lambda: service.admit(root, ctx["cid"], session="s-admin2",
                                             record=_appender(root, "s-admin2")).to_dict())


def _m_withdraw_candidate(root, ctx, scratch):
    from alpaca.wiki import service
    return _gated_call(lambda: service.withdraw_candidate(
        root, ctx["cid"], session="s-owner", record=_appender(root, "s-owner"), reason="again",
        by="owner").to_dict())


def _m_consolidate(root, ctx, scratch):
    from alpaca.wiki import service
    return _gated_call(lambda: service.consolidate(
        root, deadline_s=5.0, record=_appender(root, "job:rune2-consolidate")).to_dict())


def _m_correct(root, ctx, scratch):
    from alpaca.wiki import service
    return _gated_call(lambda: service.correct(
        root, ctx["edge"], "retract", session="s-owner", record=_appender(root, "s-owner"),
        reason="again", by="owner").to_dict())


def _m_withdraw_source(root, ctx, scratch):
    from alpaca.wiki import service
    return _gated_call(lambda: service.withdraw_source(
        root, "event:%d" % ctx["spare"], session="s-owner", record=_appender(root, "s-owner"),
        reason="spare").to_dict())


def _m_capture(root, ctx, scratch):
    from alpaca.wiki import service
    return _gated_call(lambda: service.capture(root, sessions=_sessions(root), session_events=True,
                                               deadline_s=None))


def _sort_run(root, ctx, scratch):
    from alpaca import sort
    return _gated_call(lambda: sort.run(root))


def _export(root, ctx, scratch, private=False):
    from alpaca.wiki import portable
    target = Path(scratch, "export-private.jsonl" if private else "export.jsonl")
    out = _gated_call(lambda: portable.export_store(root, target, include_private=private))
    return out + _files_text(target)


def _extract(root, ctx, scratch):
    from alpaca.wiki import extract
    target = Path(scratch, "extract")
    out = _gated_call(lambda: extract.emit(Path(root, ".alpaca", "wiki"), target))
    return out + _files_text(target)


# ------------------------------------------------------------------ the verbs
def _v(argv_of):
    def run(root, ctx, scratch):
        return cli(root, argv_of(root, ctx, scratch))
    return run


def _v_query(root, ctx, scratch):
    return "".join(cli(root, ["wiki", "query", q]).replace(q, "") for q in PLAIN_QUESTIONS)


def _v_query_refresh(root, ctx, scratch):
    q = PLAIN_QUESTIONS[0]
    return cli(root, ["--session", "s-refresh", "wiki", "query", q, "--refresh"]).replace(q, "")


def _v_context(root, ctx, scratch):
    return "".join(cli(root, ["wiki", "context", q, "--max-chars", "16000"]).replace(q, "")
                   for q in PLAIN_QUESTIONS)


def _v_context_refresh(root, ctx, scratch):
    q = PLAIN_QUESTIONS[0]
    return cli(root, ["--session", "s-refresh", "wiki", "context", q, "--refresh"]).replace(q, "")


def _v_propose(root, ctx, scratch):
    path = Path(scratch, "proposal.json")
    path.write_text(json.dumps(_proposal(root, ctx)), encoding="utf-8")
    return cli(root, ["--session", "s-p", "wiki", "propose", "--file", str(path)])


def _v_export(root, ctx, scratch):
    target = Path(scratch, "cli-export.jsonl")
    out = cli(root, ["wiki", "export", str(target)])
    private = Path(scratch, "cli-export-private.jsonl")
    out += cli(root, ["wiki", "export", str(private), "--private"])
    return out + _files_text(target) + _files_text(private)


def _v_import(root, ctx, scratch):
    """The import pours an empty store: the copy's vault is moved aside first."""
    shutil.rmtree(Path(root, ".alpaca", "wiki"), ignore_errors=True)
    return cli(root, ["wiki", "import", str(ctx["export"])])


def _m_import(root, ctx, scratch):
    from alpaca.wiki import portable
    shutil.rmtree(Path(root, ".alpaca", "wiki"), ignore_errors=True)
    return _gated_call(lambda: portable.import_store(root, ctx["export"]))


def _v_extract(root, ctx, scratch):
    target = Path(scratch, "cli-extract")
    return cli(root, ["wiki", "extract", str(Path(root, ".alpaca", "wiki")), str(target)]) \
        + _files_text(target)


def _v_backup(root, ctx, scratch):
    """`alpaca backup create`, then a restore of it into a fresh folder: the backup carries the
    record and the store as files (retained bytes, as the vault keeps them), and the restored
    project is behind its record exactly when the source was, so its serving surfaces refuse or
    serve nothing withdrawn (`ask_restored`, set by the kill test)."""
    out = cli(root, ["backup", "create"])
    backups = sorted(p for p in Path(root, ".alpaca", "backups").iterdir()
                     if p.is_dir() and not p.name.startswith((".", "verifications")))
    assert backups, out
    target = Path(scratch, "restored")
    out += cli(root, ["backup", "restore", str(backups[-1]), str(target)])
    asked = ctx.get("ask_restored")
    if asked is not None:
        Path(target, "src").mkdir(exist_ok=True)
        out += _json(asked(str(target), Path(scratch)))
    return out


#: Every reader, by registry name. A name with a space is a variant of the registered name.
RUNNERS = [
    # Memory methods (alpaca/wiki/api.py `Memory`)
    Reader("status", False, _m_status),
    Reader("query", False, _m_query),
    Reader("context", False, _m_context),
    Reader("briefing", False, _m_briefing),
    Reader("ingest", True, _m_ingest),
    Reader("record_facts_preview", False, _m_record_facts),
    Reader("propose", True, _m_propose),
    Reader("candidates", False, _m_candidates),
    Reader("admit", True, _m_admit),
    Reader("withdraw_candidate", True, _m_withdraw_candidate),
    Reader("consolidate", True, _m_consolidate),
    Reader("correct", True, _m_correct),
    Reader("withdraw_source", True, _m_withdraw_source),
    Reader("capture", True, _m_capture),
    # the functions behind verbs that are not Memory methods
    Reader("export function", False, _export),
    Reader("export function-private", False, lambda r, c, s: _export(r, c, s, private=True)),
    Reader("import function", True, _m_import),
    Reader("extract function", False, _extract),
    Reader("sort function", True, _sort_run),
    # the `wiki` verbs (alpaca/cli.py)
    Reader("recover", True, _v(lambda r, c, s: ["--session", "s-recover", "wiki", "recover"])),
    Reader("status cli", True, _v(lambda r, c, s: ["wiki", "status"])),
    Reader("query cli", True, _v_query),
    # `--refresh` runs `wiki recover` first: its report is the recover verb's (it names transcript
    # documents), the answer after it is the plain verb's on a recovered store
    Reader("recover query-refresh", True, _v_query_refresh, behind=False),
    Reader("context cli", True, _v_context),
    Reader("recover context-refresh", True, _v_context_refresh, behind=False),
    Reader("briefing cli", True, _v(lambda r, c, s: ["wiki", "briefing", "--task", "t-1",
                                                      "--max-chars", "16000", "--json"])),
    Reader("record-facts", True, _v(lambda r, c, s: ["wiki", "record-facts", "--dry-run", "--from",
                                                      "1", "--through", str(record_head(r))])),
    Reader("propose cli", True, _v_propose),
    Reader("consolidate cli", True, _v(lambda r, c, s: ["wiki", "consolidate", "--deadline-s", "5"])),
    Reader("candidates cli", True, _v(lambda r, c, s: ["wiki", "candidates"])),
    Reader("admit cli", True, _v(lambda r, c, s: ["--session", "s-admin2", "wiki", "admit", c["cid"]])),
    Reader("withdraw-candidate", True, _v(lambda r, c, s: [
        "--session", "s-owner", "wiki", "withdraw-candidate", c["cid"], "--reason", "again",
        "--by", "owner"])),
    Reader("correct cli", True, _v(lambda r, c, s: [
        "--session", "s-owner", "wiki", "correct", c["edge"], "--action", "retract", "--reason",
        "again", "--by", "owner"])),
    Reader("withdraw", True, _v(lambda r, c, s: [
        "--session", "s-owner", "wiki", "withdraw", "event:%d" % c["spare"], "--reason", "spare"])),
    Reader("export", True, _v_export),
    Reader("import", True, _v_import),
    Reader("ingest cli", True, _v(lambda r, c, s: [
        "--session", "s-ingest", "wiki", "ingest", _pier_note(s), "--doc-id", "wiki/pier-crane.md"])),
    Reader("extract", True, _v_extract),
    # other `alpaca` verbs that read the store
    Reader("collect", True, _v(lambda r, c, s: ["collect", "once"])),
    Reader("sort", True, _v(lambda r, c, s: ["sort", "--json"])),
    Reader("backup", True, _v_backup),
]


def registered_names() -> set:
    return {r.registered for r in RUNNERS}


# ------------------------------------------------------------------ running them
def withdrawn_doc_ids(root) -> list:
    """The documents of every source a withdrawal took out (`servable.excluded_source_refs`), read
    from the store at `root`, which must have processed the withdrawals."""
    from alpaca.wiki.servable import excluded_source_refs, source_refs_for_docs
    sys.path.insert(0, os.path.dirname(__file__))
    from native_helpers import store
    with store(root) as conn:
        bad = excluded_source_refs(conn)
        docs = [r[0] for r in conn.execute("SELECT doc_id FROM docs")]
        refs = source_refs_for_docs(conn, docs)
    return sorted(d for d in docs if refs.get(d) in bad)


def run_every_reader(root, scratch, ctx, *, words, doc_ids, surfaces, ask, withdrawn_bytes,
                     only=None, after_write=None, trace=None) -> dict:
    """Run every reader of `RUNNERS` (or the names in `only`) on the store at `root` and check its
    output: no word of `words` anywhere, and for a name in `surfaces` (the serving surfaces) none of
    `doc_ids` either. A reader that writes runs on a copy of `root`; after it, `ask(copy, scratch)`
    (the serving surfaces, which assert on their own) and `withdrawn_bytes(copy)` (new vault bytes
    of a withdrawn source) must be clean, and `after_write(name, copy)` runs when given. With a
    `trace` (`traced`), only the readers themselves are counted, not the checks after them. Returns
    {name: output length}."""
    quiet = trace.paused if trace is not None else contextlib.nullcontext
    scratch = Path(scratch)
    seen, problems = {}, []
    for reader in RUNNERS:
        if only is not None and reader.name not in only:
            continue
        work = scratch / ("reader-" + reader.name.replace(" ", "-"))
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        target = root
        if reader.writes:
            target = str(work / "proj")
            shutil.copytree(root, target, symlinks=True)
        try:
            before = record_head(target)
            with (contextlib.nullcontext() if reader.behind else quiet()):
                text = reader.run(target, ctx, work)
            text += _appended(target, before)
            low = text.lower()
            marks = list(words) + (list(doc_ids) if reader.registered in surfaces else [])
            leaked = [m for m in marks if m.lower() in low]
            assert not leaked, "reader %r gave withdrawn %s:\n%s" % (
                reader.name, leaked, "\n...\n".join(
                    text[max(0, low.index(m.lower()) - 700):low.index(m.lower()) + 300]
                    for m in leaked))
            if reader.writes:
                with quiet():
                    ask(target, work)
                    fresh = withdrawn_bytes(target)
                    assert not fresh, "reader %r wrote new vault bytes of a withdrawn source: %s" % (
                        reader.name, fresh)
                    if after_write is not None:
                        after_write(reader.name, target)
            seen[reader.name] = len(text)
        except AssertionError as exc:
            problems.append("%s: %s" % (reader.name, exc))
        shutil.rmtree(work, ignore_errors=True)
    assert not problems, "%d reader(s) failed on %s:\n\n%s" % (len(problems), root,
                                                                 "\n\n".join(problems))
    return seen


# ------------------------------------------------------------------ the enumeration
#: Calls that open the Rune2 store, and calls that render record events as notes or record facts.
STORE_OPENERS = frozenset({"open_db_readonly", "DB"})
FACT_RENDERERS = frozenset({"_note_doc", "_note_text", "normalize_events"})
#: The serving gate (`service._removal_acts_pending`, `service._serving_gate`).
GATES = frozenset({"_serving_gate", "_removal_acts_pending"})


def _own_nodes(fn):
    """The nodes of `fn`'s body, not those of a function or class defined inside it."""
    stack = list(fn.body) + list(fn.decorator_list)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _imports(tree, modname: str, is_package: bool) -> dict:
    """local name -> dotted target of every import in a module (at any depth): a module
    (`alpaca.wiki.service`) or a module's attribute (`alpaca.wiki.engine.answer.answer`)."""
    package = modname.split(".") if is_package else modname.split(".")[:-1]
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                stem = package[:len(package) - (node.level - 1)]
                mod = ".".join(stem + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            for a in node.names:
                out[a.asname or a.name] = mod + "." + a.name
    return out


def _dotted(node):
    """`a.b.c` of a chain of names and attributes, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _target(func, imports, modname, top) -> str:
    """The dotted name a call's function resolves to: through the module's imports, to a top-level
    definition of the module itself, or `*.<name>` for a method of an object not known here."""
    dotted = _dotted(func)
    if dotted is None:
        return "*." + func.attr if isinstance(func, ast.Attribute) else "?"
    head, _, rest = dotted.partition(".")
    if head in imports:
        return imports[head] + ("." + rest if rest else "")
    if not rest:
        return "%s.%s" % (modname, head) if head in top else "?." + head
    return "*." + dotted.rsplit(".", 1)[1]


def why_reads(fn, imports, modname, top) -> tuple:
    """(reasons, gated, calls, targets): why `fn` reads store rows or record fact text (empty when it
    does not), whether its own body consults the serving gate, the names it calls and the dotted
    targets they resolve to (`_target`). It reads when it opens the store (`STORE_OPENERS`),
    connects to the store's file by hand, renders record events as notes (`FACT_RENDERERS`) or
    extracts record facts (`record_extract.extract`)."""
    reasons, calls, targets, gated = set(), set(), set(), False
    nodes = list(_own_nodes(fn))
    funcs = {id(n.func) for n in nodes if isinstance(n, ast.Call)}
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        target = _target(node.func, imports, modname, top)
        targets.add(target)
        name = target.rsplit(".", 1)[-1]
        calls.add(name)
        if name in STORE_OPENERS:
            reasons.add("opens the store (%s)" % name)
        if name in FACT_RENDERERS:
            reasons.add("renders record events (%s)" % name)
        if name == "extract" and (target.endswith("record_extract.extract")
                                  or (_dotted(node.func) or "").endswith("record_extract.extract")):
            reasons.add("extracts record facts (record_extract.extract)")
        if name in GATES:
            gated = True
    # a connection opened by hand on the store: a `connect` call in a function that names the store
    # file or the wiki config's `db_path` (a stat or an existence check of the path reads no row)
    names_store = any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                      and n.value.rsplit("/", 1)[-1] == "rune.db" for n in nodes) or \
        any(isinstance(n, ast.Attribute) and n.attr == "db_path" and id(n) not in funcs for n in nodes)
    if names_store and calls & {"connect", "_read_db"}:
        reasons.add("connects to the store by its path")
    return sorted(reasons), gated, calls, targets


def _functions(tree, prefix=""):
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            yield from ((q, f, c or node.name) for q, f, c in _functions(node, prefix + node.name + "."))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield prefix + node.name, node, None
            yield from _functions(node, prefix + node.name + ".<locals>.")


def module_name(rel: str) -> str:
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def product_paths(package=None) -> list:
    package = Path(package or REPO / "alpaca")
    return [p for p in sorted(package.rglob("*.py")) if "tests" not in p.relative_to(package).parts]


def scan(paths, base=REPO) -> dict:
    """Every function of `paths`: {(rel, qualname): info} with `line` (its code object's first line),
    `reasons`, `gated`, `calls`, `targets` (`why_reads`), `names` (the dotted names a call to it
    resolves to: `<module>.<function>`, `<module>.<Class>` for `__init__`, `*.<method>` for a
    method) and `strings` (the `module:function` strings that name it, a target a runner starts)."""
    out, strings = {}, set()
    for path in paths:
        path = Path(path)
        rel = path.relative_to(base).as_posix() if path.is_absolute() else path.as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        modname = module_name(rel)
        imports = _imports(tree, modname, path.name == "__init__.py")
        top = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                           ast.ClassDef))}
        strings.update(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                       and isinstance(n.value, str) and ":" in n.value)
        for qual, fn, cls in _functions(tree):
            reasons, gated, calls, targets = why_reads(fn, imports, modname, top)
            first = min([fn.lineno] + [d.lineno for d in fn.decorator_list])
            if cls and fn.name == "__init__":
                names = {"%s.%s" % (modname, cls)}
            elif cls:
                names = {"%s.%s.%s" % (modname, cls, fn.name), "*." + fn.name}
            else:
                names = {"%s.%s" % (modname, fn.name)} if "<locals>" not in qual else \
                    {"?." + fn.name}
            out[(rel, qual)] = {"line": first, "def": fn.lineno, "reasons": reasons, "gated": gated,
                                "calls": calls, "targets": targets, "names": names,
                                "module": modname, "path": str(path)}
    for info in out.values():
        info["strings"] = {t for t in strings
                           if any(t == n.rsplit(".", 1)[0] + ":" + n.rsplit(".", 1)[1]
                                  for n in info["names"] if not n.startswith(("*", "?")))}
    return out


def store_readers(paths, base=REPO) -> dict:
    """The functions of `paths` that read store rows or record fact text (`why_reads`)."""
    return {k: v for k, v in scan(paths, base).items() if v["reasons"]}


def unreachable_modules(paths, base=REPO) -> set:
    """Modules of `paths` that no other module of `paths` imports or names in a string (a dotted
    target such as `alpaca.wiki.service:capture`), that are not a CLI verb module
    (`cli.VERB_MODULES`), a hook, a package `__init__` or `__main__`: code no product entry runs."""
    names = {module_name(Path(p).relative_to(base).as_posix()): Path(p) for p in paths}
    referenced = set()
    for name, path in names.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = _imports(tree, name, path.name == "__init__.py")
        for target in imports.values():
            referenced.add(target)
            referenced.add(target.rsplit(".", 1)[0])
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                referenced.add(node.value.split(":", 1)[0])
    from alpaca import cli as alpaca_cli
    entries = {"alpaca." + m for m in alpaca_cli.VERB_MODULES}
    out = set()
    for name, path in names.items():
        if name in referenced or name in entries or path.name in ("__init__.py", "__main__.py") \
                or name.startswith("alpaca.hooks."):
            continue
        if any(r.startswith(name + ".") for r in referenced):
            continue
        out.add(name)
    return out


def callers(key, functions) -> list:
    """The product functions whose calls resolve to the function `key` (`scan` `names`)."""
    names = functions[key]["names"]
    return [k for k, v in functions.items() if k != key and v["targets"] & names]


def upstream_gated(key, functions, readers, gated_keys, _seen=None) -> bool:
    """Whether every product function that calls the function `key` is gated itself or, in turn,
    called only from gated functions; False with no caller, or when a `module:function` string names
    it (a target some runner starts, a caller this check cannot see)."""
    _seen = set() if _seen is None else _seen
    if key in _seen or functions[key]["strings"]:
        return False
    _seen = _seen | {key}
    found = callers(key, functions)
    if not found:
        return False
    return all(c in gated_keys or upstream_gated(c, functions, readers, gated_keys, _seen)
               for c in found)


# ------------------------------------------------------------------ the trace
class Trace:
    """Which of the targets started running while the trace was not paused (`seen`)."""

    def __init__(self):
        self.seen = set()
        self.active = True

    @contextlib.contextmanager
    def paused(self):
        """Code run here is not counted: the checks after a reader, which ask a store that may be
        current by then (`wiki recover` ran)."""
        before, self.active = self.active, False
        try:
            yield
        finally:
            self.active = before


@contextlib.contextmanager
def traced(targets: dict):
    """Record which of `targets` ({(realpath, first line): key}) start running in this process
    (sys.monitoring PY_START) outside `Trace.paused`; yields the `Trace`."""
    mon = sys.monitoring
    tool = next(i for i in range(6) if mon.get_tool(i) is None)
    mon.use_tool_id(tool, "rune2-store-readers")
    trace = Trace()
    paths = {}

    def start(code, offset):
        real = paths.get(code.co_filename)
        if real is None:
            real = paths[code.co_filename] = os.path.realpath(code.co_filename)
        key = targets.get((real, code.co_firstlineno))
        if key is None:
            return mon.DISABLE
        if not trace.active:
            return None                      # keep the location live for a later, counted start
        trace.seen.add(key)
        return mon.DISABLE

    mon.register_callback(tool, mon.events.PY_START, start)
    mon.set_events(tool, mon.events.PY_START)
    mon.restart_events()
    try:
        yield trace
    finally:
        mon.set_events(tool, 0)
        mon.register_callback(tool, mon.events.PY_START, None)
        mon.free_tool_id(tool)
        mon.restart_events()
