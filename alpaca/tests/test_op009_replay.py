"""op-019 t-204 (brief section 4): the start of op-009 replayed through the start-of-op batch.

The plan is op-009's own: the execution plan the owner was shown, transcript c2903526 line 507
(alpaca/tests/fixtures/op009-replay/plan.md, copied byte for byte). The batch must ask how the agents
run (the `runtime` class item, there whatever the plan says) and the plan's own open question about
the loop runner, and an L6 start must refuse while either is unanswered. The door half of the replay
(a fresh checker reading op-009's architecture.md) needs a live agent; it is run by
.alpaca/proofs/op-019/logs/t-204/replay.py and recorded in the t-204 proof.
"""
import os

from alpaca import batch, intentgate
from alpaca.tests.test_batch import level, new_op, p, run, task  # noqa: F401 (fixture p)

PLAN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "op009-replay", "plan.md")


def test_the_op009_plan_through_the_batch_asks_how_the_agents_run_and_l6_refuses(p):
    op = new_op(p, "an autoresearch loop whose results can be trusted")
    code, out = run(p, "batch", "open", op, "--plan", PLAN)
    assert code == 0, out
    items = batch.items(p.conn, op)
    questions = {name: (item.get("question") or item.get("text") or "") for name, item in items.items()}
    assert "runtime" in items
    loop = [n for n, q in questions.items()
            if "Does the loop runner run as a background Alpaca process or inside the chat?" in q]
    assert loop, questions
    t = task(p, op)
    level(p, 6)
    code, out = run(p, "task", "claim", t, "--by", "claude")
    assert code != 0
    assert "BATCH-UNANSWERED" in out and "runtime" in out and loop[0] in out
