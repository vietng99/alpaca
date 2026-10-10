"""Shared builders for the research doctor tests (t-115, alpaca/tests/test_research_doctor.py). A
test module, not a conftest: import it. Synthetic data only: a made-up pack `synthpack` with one
format 3 runbook whose defaults file (pack.yaml) holds the tool declaration
research.fingerprint.tools, laid into the `project` fixture root of alpaca/tests/conftest.py.

Tool names are invented (nosuchtool-t115*) so no host can carry them. Every fixture is built in
tmp_path; nothing is laid under alpaca/tests/fixtures/, where catalog.find would list a tracked
missing-tool runbook as a pack of this repository."""
from __future__ import annotations

import os
import subprocess

from alpaca.tests import research_loopkit as kit

PACK = "synthpack"
PACK_DIR = "packs/synthpack"
RUNBOOK = PACK_DIR + "/rb.yaml"
MISSING = "nosuchtool-t115a"

_RB_TAIL = "  fingerprint:\n    tools: [sh]\n"


def runbook_text(runbook_tools=None, extra_research=""):
    """The loop kit's improve runbook as a pack runbook: `defaults: pack.yaml` added, its own
    fingerprint block dropped (so the pack layer declares the tools) unless runbook_tools is given."""
    text = (kit.RB.replace("ROUNDS", "1").replace("SLOTS", "1").replace("RETRIES", "1")
            .replace("DISK", "0.001").replace("USD", "10"))
    assert text.endswith(_RB_TAIL), "loop kit runbook changed shape"
    text = text[: -len(_RB_TAIL)]
    text = text.replace("  mode: improve\n", "  mode: improve\n  defaults: pack.yaml\n", 1)
    text = text.replace("sh ${CONTRACT_DIR}/pack/measure.sh", "sh ${CONTRACT_DIR}/%s/measure.sh" % PACK_DIR)
    text = text.replace("protected: [pack/]", "protected: [%s/]" % PACK_DIR)
    if runbook_tools is not None:
        text += "  fingerprint:\n    tools: [%s]\n" % ", ".join(runbook_tools)
    return text + extra_research


def pack_yaml(tools):
    if tools is None:
        return "pack: %s\nresearch:\n  disk: 1\n" % PACK
    return "pack: %s\nresearch:\n  fingerprint:\n    tools: [%s]\n" % (PACK, ", ".join(tools))


def lay_pack(root, tools=(MISSING,), runbook_tools=None, extra_research="", where=PACK_DIR):
    """The synthetic pack under root/<where>. Returns the runbook path relative to root."""
    kit.write(root, where + "/pack.yaml", pack_yaml(None if tools is None else list(tools)))
    kit.write(root, where + "/spec.md", kit.SPEC)
    kit.write(root, where + "/measure.sh", kit.MEASURE, 0o755)
    kit.write(root, where + "/rb.yaml", runbook_text(runbook_tools, extra_research)
              .replace(PACK_DIR, where))
    if not os.path.exists(os.path.join(root, "src", "value.txt")):
        kit.write(root, "src/value.txt", "100\n")
    return where + "/rb.yaml"


def commit(root, ignore=".alpaca/\ncontracts/research/\n"):
    kit.write(root, ".gitignore", ignore)
    if not os.path.isdir(os.path.join(root, ".git")):
        kit.git(root, "init", "-q", "-b", "main")
    kit.git(root, "add", "-A")
    kit.git(root, "commit", "-q", "-m", "base", "--allow-empty")


def text(row):
    """One doctor row as one string, whatever shape it has (dict from alpaca.doctor.checks, 3-tuple
    from a check module)."""
    if isinstance(row, dict):
        return " ".join(str(row.get(k, "")) for k in ("name", "level", "detail"))
    return " ".join(str(x) for x in row)


def level(row):
    return row.get("level") if isinstance(row, dict) else row[1]


def rows_naming(rows, *needles, levels=("error",)):
    return [r for r in rows if level(r) in levels and all(n in text(r) for n in needles)]


def fmt3_findings(root, rel):
    """(errors, warnings) codes of the runbook's research block, so a fixture that is not a valid
    format 3 runbook is caught by the attack itself, not mistaken for a doctor result."""
    from alpaca import runbook
    path = os.path.join(root, rel)
    data, rep = runbook.load(path)
    assert isinstance(data, dict), [e for e in rep.errors]
    block, _l, report = runbook.expand_research(data, os.path.dirname(path))
    errs, warns = runbook.research_findings(data, block, os.path.dirname(path), check_files=True,
                                            repo_root=root)
    return ([e.get("code") for e in list(report.errors) + list(errs)],
            [w.get("code") for w in warns], block)


def onboarded(project):
    """The doctor test project of alpaca/tests/test_doctor.py, initialised and onboarded, with its
    transcript folder: `alpaca doctor` exits 0 on it at HEAD."""
    from alpaca import cli, doctor
    from alpaca.tests.test_doctor import _full
    _full(project)
    cli.main(["init"])
    cli.main(["onboard", "--name", "d", "--who", "a:owner", "--what", "w"])
    os.makedirs(doctor.paths.transcript_dir(project), exist_ok=True)
    return project


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)
