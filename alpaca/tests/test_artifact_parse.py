"""M1.9 - acceptance-table parser (alpaca/checklist/artifact.py).

Proof test for the Done-when: a spec file whose acceptance table has residue outside
the table is refused with BLOCKED, and a clean table yields the exact item set keyed
on AC-nn. A fixture pair (clean / residue) plus the stray-line, duplicate-key and
empty-table refusals of Step 1, and the Step 4 assertion that the shipped engineering
example is refused as a store by design.
"""
import os

import pytest

from alpaca.checklist import Halt, artifact
from alpaca.gates import verdict

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CLEAN = """# Sample spec

Some prose that names no obligation at all.

## Acceptance

| item  | statement                          | oracle class | proof kind |
| ----- | ---------------------------------- | ------------ | ---------- |
| AC-01 | the parser reads a clean table     | unit         | test       |
| AC-02 | residue outside the table is BLOCK | unit         | test       |
| AC-03 | keys are unique inside the table   | unit         | test       |

## Open questions

None.
"""

# Same clean table, but one AC-shaped obligation lives in prose OUTSIDE the table.
RESIDUE = """# Sample spec

The build also has to satisfy AC-99, which nobody wrote a row for.

## Acceptance

| item  | statement                          | oracle class | proof kind |
| ----- | ---------------------------------- | ------------ | ---------- |
| AC-01 | the parser reads a clean table     | unit         | test       |
| AC-02 | residue outside the table is BLOCK | unit         | test       |

## Open questions

None.
"""

# A stray prose line wedged between two body rows breaks the partition.
STRAY = """# Sample spec

## Acceptance

| item  | statement                       | oracle class | proof kind |
| ----- | ------------------------------- | ------------ | ---------- |
| AC-01 | first obligation                | unit         | test       |
this stray line does not belong between the rows
| AC-02 | second obligation               | unit         | test       |
"""

DUPLICATE = """# Sample spec

## Acceptance

| item  | statement                       | oracle class | proof kind |
| ----- | ------------------------------- | ------------ | ---------- |
| AC-01 | first obligation                | unit         | test       |
| AC-01 | a second row reusing the key    | unit         | test       |
"""

EMPTY = """# Sample spec

## Acceptance

| item  | statement | oracle class | proof kind |
| ----- | --------- | ------------ | ---------- |

## Open questions

None.
"""


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# ------------------------------------------------------------------ positive path
def test_clean_table_yields_exact_ac_item_set(tmp_path):
    path = _write(tmp_path, "clean.md", CLEAN)
    result = artifact.parse(path, "item")
    keys = set(row["key"] for row in result["items"])
    assert keys == {"AC-01", "AC-02", "AC-03"}
    assert result["key_column"] == "item"
    # the cells of a parsed row carry the declared columns of the acceptance table
    row = next(r for r in result["items"] if r["key"] == "AC-01")
    assert row["cells"]["statement"] == "the parser reads a clean table"
    assert row["cells"]["oracle class"] == "unit"


# ------------------------------------------------------------------ negative path (headline)
def test_residue_outside_the_table_is_blocked(tmp_path):
    path = _write(tmp_path, "residue.md", RESIDUE)
    with pytest.raises(Halt) as ei:
        artifact.parse(path, "item")
    assert ei.value.verdict == verdict.BLOCKED


# ------------------------------------------------------------------ Step 1 refusals
def test_stray_line_between_rows_is_refused(tmp_path):
    path = _write(tmp_path, "stray.md", STRAY)
    with pytest.raises(Halt):
        artifact.parse(path, "item")


def test_duplicate_key_is_refused(tmp_path):
    path = _write(tmp_path, "dup.md", DUPLICATE)
    with pytest.raises(Halt):
        artifact.parse(path, "item")


def test_empty_table_is_blocked_never_a_pass(tmp_path):
    path = _write(tmp_path, "empty.md", EMPTY)
    with pytest.raises(Halt) as ei:
        artifact.parse(path, "item")
    assert ei.value.verdict == verdict.BLOCKED


def test_no_key_column_table_is_blocked(tmp_path):
    path = _write(tmp_path, "nokey.md", CLEAN)
    with pytest.raises(Halt) as ei:
        artifact.parse(path, "req_key")  # no table carries this column
    assert ei.value.verdict == verdict.BLOCKED


# ------------------------------------------------------------------ Step 4: the shipped example
def test_engineering_example_is_refused_as_a_store_by_design():
    example = os.path.join(REPO, "templates", "checklist.example.md")
    assert os.path.isfile(example)
    with pytest.raises(Halt) as ei:
        artifact.parse(example, "item")
    assert ei.value.verdict == verdict.BLOCKED
