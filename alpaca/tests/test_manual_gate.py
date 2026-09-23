"""M4.16 proof: the user manual and its truth gate.

Clauses 1, 3, 4, 5, 6 and 7 (clause 2 is `test_manual_ascii.py`). Every clause is a byte check on
the SHIPPED `docs/manual.html` and the tree, and every check is driven on a POSITIVE and a
NEGATIVE path, so no clause is a tautological pass: an unknown path, an unknown verb, an
undocumented front verb, an undocumented hook, an unclosed tag, a second <body>, a reordered head,
a missing viewport, a dark palette block short one property, a fixed width above 400px, and an
ungoverned table must each be caught.

Step 4 is proved too: the manual gate is a release-door link, so a failing manual closes the door
(clause 7); and `alpaca op close` for the M4 op refuses (exit 3) without the owner's
`manual-phone-read` countersign and reads ONLY that ref (not `render-neutralisation`).
"""
import importlib.util
import json
import os
import shutil

import pytest

from alpaca import cli, db
from alpaca.gates import verdict as vc
from alpaca.phase import doors
from alpaca.tests.conftest import REPO

HTML_FILE = os.path.join(REPO, "docs", "manual.html")
MANUAL_FILE = os.path.join(REPO, "MANUAL.md")
SETTINGS_FILE = os.path.join(REPO, ".claude", "settings.json")

HOOKS = (
    ("SessionStart", "session_start"),
    ("UserPromptSubmit", "user_prompt"),
    ("PreToolUse", "pre_tool"),
    ("PostToolUse", "post_tool"),
    ("Stop", "stop"),
    ("SubagentStop", "subagent_stop"),
    ("SessionEnd", "session_end"),
    ("PreCompact", "pre_compact"),
)


def _load():
    path = os.path.join(REPO, "setup", "build_manual_html.py")
    spec = importlib.util.spec_from_file_location("alpaca_build_manual_html_gate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


@pytest.fixture(scope="module")
def html():
    with open(HTML_FILE, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def manual():
    with open(MANUAL_FILE, encoding="utf-8") as fh:
        return fh.read()


# =============================================================== the whole gate over the tree
def test_shipped_manual_passes_the_full_gate(mod):
    findings = mod.gate(REPO)
    assert findings == [], "the shipped manual must pass every clause: %r" % findings


# =============================================================== clause 1: truth vs the tree
def test_clause1_shipped_manual_resolves_in_the_tree(mod):
    assert mod.check_tree(REPO) == []


def test_clause1_every_front_verb_is_documented(manual):
    from alpaca import surface
    for verb in surface.FRONT:
        assert ("`alpaca %s`" % verb) in manual, "front verb alpaca %s is not in MANUAL.md" % verb


def test_clause1_every_registered_hook_is_documented(mod, manual):
    with open(SETTINGS_FILE, encoding="utf-8") as fh:
        data = json.load(fh)
    events = set((data.get("hooks") or {}).keys())
    assert events, "settings.json registers no hooks"
    modules = set()
    for groups in (data.get("hooks") or {}).values():
        for group in groups or []:
            for hook in group.get("hooks", []) or []:
                import re
                for m in re.finditer(r"alpaca\.hooks\.([a-z_]+)", hook.get("command", "")):
                    modules.add("alpaca.hooks.%s" % m.group(1))
    for event in events:
        assert event in manual, "hook event %s is not named in MANUAL.md" % event
    for module in modules:
        assert module in manual, "hook module %s is not named in MANUAL.md" % module


def _full_manual_text():
    """A minimal manual that documents every front verb and every registered hook and names no
    file path, so a defect injected on top of it is the ONLY finding check_tree reports."""
    from alpaca import surface
    lines = ["# Alpaca user manual", "", "## Verbs", ""]
    for verb in surface.FRONT:
        lines.append("- `alpaca %s` - a front verb." % verb)
    lines += ["", "## Hooks", ""]
    for event, module in HOOKS:
        lines.append("- %s runs `alpaca.hooks.%s`." % (event, module))
    return "\n".join(lines) + "\n"


def test_clause1_catches_an_unknown_path(mod):
    text = _full_manual_text() + "\nSee `alpaca/does_not_exist.py` for details.\n"
    findings = mod.check_tree(REPO, manual_text=text)
    assert any(f["reason"] == "unknown-path" for f in findings), findings


def test_clause1_catches_an_unknown_verb(mod):
    text = _full_manual_text() + "\nRun `alpaca bogusverb` to lose.\n"
    findings = mod.check_tree(REPO, manual_text=text)
    assert any(f["reason"] == "unknown-verb" for f in findings), findings


def test_clause1_catches_an_undocumented_front_verb(mod):
    from alpaca import surface
    victim = surface.FRONT[0]
    text = _full_manual_text().replace("- `alpaca %s` - a front verb.\n" % victim, "")
    findings = mod.check_tree(REPO, manual_text=text)
    assert any(f["reason"] == "front-verb-undocumented" for f in findings), findings


def test_clause1_catches_an_undocumented_hook(mod):
    text = _full_manual_text().replace("`alpaca.hooks.session_start`", "the first hook")
    findings = mod.check_tree(REPO, manual_text=text)
    assert any(f["reason"] in ("hook-undocumented", "unknown-hook") for f in findings), findings


def test_clause1_full_manual_text_is_clean(mod):
    # the base text (front verbs + hooks, no stray path) must itself be clean, or the negatives
    # above would not be isolating one defect.
    assert mod.check_tree(REPO, manual_text=_full_manual_text()) == []


# =============================================================== clause 3: parseable HTML
def test_clause3_shipped_page_parses(mod, html):
    assert mod.check_parse(html) == []


def test_clause3_catches_an_unclosed_tag(mod, html):
    broken = html.replace("</main>", "", 1)
    findings = mod.check_parse(broken)
    assert any(f["reason"] in ("unclosed-tag", "tag-mismatch") for f in findings), findings


def test_clause3_catches_a_second_body(mod, html):
    broken = html.replace("</body>", "<body></body></body>", 1)
    findings = mod.check_parse(broken)
    assert any(f["reason"] == "wrong-count" for f in findings), findings


def test_clause3_catches_a_missing_html_root(mod, html):
    broken = html.replace("<html lang=\"en\">", "", 1)
    findings = mod.check_parse(broken)
    assert any(f["reason"] == "wrong-count" for f in findings), findings


# =============================================================== clause 4: the head metas
def test_clause4_shipped_page_has_the_metas(mod, html):
    assert mod.check_metas(html) == []


def test_clause4_catches_a_missing_viewport(mod, html):
    broken = html.replace(
        '<meta name="viewport" content="width=device-width, initial-scale=1">', "", 1)
    findings = mod.check_metas(broken)
    assert any(f["reason"].startswith("viewport") for f in findings), findings


def test_clause4_catches_charset_not_first(mod, html):
    # push a <title> in front of the charset meta.
    broken = html.replace('<meta charset="utf-8">',
                          '<title>early</title>\n<meta charset="utf-8">', 1)
    findings = mod.check_metas(broken)
    assert any(f["reason"] == "charset-not-first" for f in findings), findings


# =============================================================== clause 5: the dual-theme palette
def test_clause5_three_blocks_share_one_property_set(mod, html):
    assert mod.check_palette(html) == []


def test_clause5_catches_a_dark_block_missing_a_property(mod, html):
    import re
    m = re.search(r':root\[data-theme="dark"\]\s*\{', html)
    inner, end = mod._brace_block(html, m.end() - 1)
    # drop the first custom property declaration inside the data-theme dark block.
    dropped = re.sub(r"--[A-Za-z0-9_-]+:[^;]*;", "", inner, count=1)
    broken = html[:m.end()] + dropped + html[end:]
    findings = mod.check_palette(broken)
    assert any(f["reason"] == "palette-drift" for f in findings), findings


def test_clause5_catches_a_missing_media_dark_block(mod, html):
    broken = html.replace("@media (prefers-color-scheme: dark)",
                          "@media (min-width: 1px)", 1)
    findings = mod.check_palette(broken)
    assert findings, "a page with no prefers-color-scheme dark block must fail clause 5"


# =============================================================== clause 6: phone layout
def test_clause6_shipped_page_layout_is_sound(mod, html):
    assert mod.check_layout(html) == []


def test_clause6_catches_a_fixed_width_over_400px(mod, html):
    broken = html.replace(".wrap { max-width: 760px;",
                          ".wrap { min-width: 520px; max-width: 760px;", 1)
    findings = mod.check_layout(broken)
    assert any(f["reason"] == "fixed-width" for f in findings), findings


def test_clause6_catches_an_ungoverned_table(mod):
    page = ("<html><head><style>body{color:#000}</style></head>"
            "<body><table><tr><td>x</td></tr></table></body></html>")
    findings = mod.check_layout(page)
    assert any(f["reason"] == "ungoverned-element" for f in findings), findings


def test_clause6_governed_table_passes(mod):
    page = ("<html><head><style>table{max-width:100%;overflow-x:auto}</style></head>"
            "<body><table><tr><td>x</td></tr></table></body></html>")
    assert mod.check_layout(page) == []


# =============================================================== clause 7: the release door
def _passing_release_root(project, mod):
    """Turn the throwaway project into a tree the manual gate PASSES: the renderer, the hooks, the
    settings and a minimal-but-complete manual, with docs/manual.html rendered from it."""
    root = project
    os.makedirs(os.path.join(root, ".claude"), exist_ok=True)
    shutil.copy(SETTINGS_FILE, os.path.join(root, ".claude", "settings.json"))
    os.makedirs(os.path.join(root, "setup"), exist_ok=True)
    shutil.copy(os.path.join(REPO, "setup", "build_manual_html.py"),
                os.path.join(root, "setup", "build_manual_html.py"))
    hooks_dir = os.path.join(root, "alpaca", "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    src_hooks = os.path.join(REPO, "alpaca", "hooks")
    for name in os.listdir(src_hooks):
        if name.endswith(".py"):
            shutil.copy(os.path.join(src_hooks, name), os.path.join(hooks_dir, name))
    with open(os.path.join(root, "MANUAL.md"), "w", encoding="utf-8") as fh:
        fh.write(_full_manual_text())
    mod.build(root)
    return root


def test_clause7_manual_gate_code_reads_the_tree(mod):
    # the real tree passes; a tree with no manual fails (the gate cannot pass what it cannot read).
    assert doors.manual_gate_code(root=REPO) == vc.PASS


def test_clause7_a_failing_manual_closes_the_release_door(project, mod):
    root = _passing_release_root(project, mod)
    cli.main(["init"])
    conn = db.connect(root)
    doors.record_countersign(conn, "render-neutralisation",
                             ["RESUME.md", "analytics/index.html"],
                             pointer="journal:owner read both", root=root)
    # a clean manual + the render-neutralisation countersign opens the door.
    assert doors.manual_gate_code(root=root) == vc.PASS
    assert doors.release_door(conn, "op-001", root=root, include_manual=True) == vc.PASS
    # break the shipped page: a candidate whose manual gate fails does not open the door.
    with open(os.path.join(root, "docs", "manual.html"), "w", encoding="utf-8") as fh:
        fh.write("<html><head><title>x</title></head><body></body></html>\n")
    assert doors.manual_gate_code(root=root) == vc.FAIL
    assert doors.release_door(conn, "op-001", root=root, include_manual=True) == vc.FAIL


def test_clause7_door_without_include_manual_is_unchanged(project, mod):
    # the manual link is opt-in: a door that does not include it behaves as before (M4.15).
    root = _passing_release_root(project, mod)
    cli.main(["init"])
    conn = db.connect(root)
    # break the manual, but do not include it: the countersign still governs the door.
    with open(os.path.join(root, "docs", "manual.html"), "w", encoding="utf-8") as fh:
        fh.write("<html broken>\n")
    assert doors.release_door(conn, "op-001", root=root) == vc.PAUSED  # no countersign yet
    doors.record_countersign(conn, "render-neutralisation", ["RESUME.md"], root=root)
    assert doors.release_door(conn, "op-001", root=root,
                              extra_links=[("packaging", vc.PASS)]) == vc.PASS


# =============================================================== Step 4: alpaca op close for the M4 op
def test_m4_op_close_refuses_without_manual_phone_read(project, capsys):
    cli.main(["init"])
    cli.main(["op", "new", "M4 milestone: packaging and the user manual",
              "--done-when", "green"])
    code = cli.main(["op", "close", "op-001", "--basis", "done"])
    assert code == vc.PAUSED
    out = capsys.readouterr().out
    assert "manual-phone-read" in out, out


def test_m4_op_close_succeeds_with_manual_phone_read(project):
    cli.main(["init"])
    cli.main(["op", "new", "M4 milestone: packaging and the user manual",
              "--done-when", "green"])
    conn = db.connect(project)
    doors.record_countersign(conn, "manual-phone-read",
                             ["iPhone 15, Safari, file://, 2026-09-17"],
                             pointer="journal:owner read the manual on a phone", root=project)
    conn.close()
    assert cli.main(["op", "close", "op-001", "--basis", "done"]) == vc.PASS


def test_m4_op_close_ignores_render_neutralisation(project):
    cli.main(["init"])
    cli.main(["op", "new", "M4 milestone: packaging and the user manual",
              "--done-when", "green"])
    conn = db.connect(project)
    # only the OTHER countersign is on the record; it belongs to the release door, not this close.
    doors.record_countersign(conn, "render-neutralisation", ["RESUME.md"], root=project)
    conn.close()
    assert cli.main(["op", "close", "op-001", "--basis", "done"]) == vc.PAUSED


def test_non_m4_op_close_is_unaffected(project):
    cli.main(["init"])
    cli.main(["op", "new", "ship the widget", "--done-when", "green"])
    assert cli.main(["op", "close", "op-001", "--basis", "done"]) == vc.PASS
