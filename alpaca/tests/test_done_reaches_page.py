"""A task closed the normal way must reach the Work page with its report.

The chain: `task add --title`, `proof new`, `proof seal`, `task move done --proof`, then the
server's `/hub/overview.json` projection. A seal enforces the proof report's section headings;
the page reads three of them by name. If either side renames a heading, cards go blank without
an error, so this file pins both sides together and walks the whole chain once.
"""
from alpaca import cli, hub, proof, work_record
from alpaca.tests import proofkit


def test_card_reads_the_sections_a_seal_enforces():
    card = {heading.casefold(): key for heading, key in work_record._SECTIONS.items()}
    for heading, key in (("What I did", "what"), ("How I did it", "how"), ("Result", "result")):
        assert heading in proof.REQUIRED, heading
        assert card.get(heading.casefold()) == key, heading


def test_done_task_shows_title_report_and_completion(project):
    cli.main(["init"])
    cli.main(["op", "new", "x"])
    assert cli.main(["task", "add", "op-001", "B.0 cva6 first physical run: route DRC 0, KLayout DRC 0",
                     "--title", "cva6 first GDS"]) == 0
    before = next(t for t in hub.overview(project)["tasks"] if t["id"] == "t-001")
    assert before["status"] == "open" and before["report"] is None
    pointer = proofkit.seal_for(project, "t-001")
    assert cli.main(["task", "move", "t-001", "done", "--proof", pointer]) == 0
    item = next(t for t in hub.overview(project)["tasks"] if t["id"] == "t-001")
    assert item["status"] == "done"
    assert item["title"] == "cva6 first GDS"
    assert item["description"].startswith("B.0 cva6 first physical run")
    report = item["report"]
    assert report and report["sealed"] is True
    assert report["what"] and report["how"] and report["result"]
    assert item["completed_at"]
