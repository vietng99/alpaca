"""Every file under alpaca/web/vendor/ is named in THIRD-PARTY-NOTICES.md (op-008 t-072).

The Arthur avatar package reached the release candidate with no entry in the notices file, whose
first paragraph promises that it lists every file that comes from somewhere else and that a file
not listed is Alpaca's own. The vendor folder is where such files land, so each of its files must be
named there, by its own path or by a `folder/**` entry that covers it. The license and provenance
notes of the folder are the exception: the notices file points at them instead.
"""
import re
from pathlib import Path

from alpaca.tests import conftest

REPO = Path(conftest.REPO)

VENDOR = REPO / "alpaca" / "web" / "vendor"
NOTES = {"README.md", "OFL.txt", "world-atlas-LICENSE.txt"}


def _covered(rel, notices):
    if "`%s`" % rel in notices:
        return True
    for folder in re.findall(r"`([^`*]+)/\*\*`", notices):
        if rel.startswith(folder + "/"):
            return True
    return False


def test_every_vendored_web_file_is_named_in_the_notices():
    notices = (REPO / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    files = [p for p in sorted(VENDOR.rglob("*")) if p.is_file() and "__pycache__" not in p.parts]
    assert files
    missing = [p.relative_to(REPO).as_posix() for p in files
               if p.name not in NOTES and not _covered(p.relative_to(REPO).as_posix(), notices)]
    assert missing == []


def test_the_arthur_entry_says_whose_work_it_is():
    notices = (REPO / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    section = notices.split("## Arthur", 1)[1].split("\n## ", 1)[0]
    assert "owner's own artwork" in section
    assert "`alpaca/web/vendor/arthur/**`" in section
