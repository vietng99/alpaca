"""Refusals and reason codes of the research engine. Exit codes come from alpaca.gates.verdict
and are never restated. Design: architecture.md section 12.4; the RESEARCH-* verb codes are
operator-surface.md section 4.4's and the CONTRACT-* loader codes runbook-format-3.md section
6.2's."""
from __future__ import annotations

from alpaca.gates import verdict as vc


class ResearchError(Exception):
    """Base of every engine error."""


class ResearchRefusal(ResearchError):
    def __init__(self, code: int, reason: str, detail: str = ""):
        super().__init__("%s %s: %s" % (vc.name_of(code), reason, detail))
        self.code, self.reason, self.detail = int(code), reason, detail


class ContractRefusal(ResearchRefusal):
    """A contract the loader refuses. For CONTRACT-LOOSEN-UNGRANTED, `loosened` holds every
    ungranted loosening of both layers (contract.check_override, section 3.5), each a dict
    {"ref", "field", "accepted", "new"}: ref the contract ref the field's grant belongs on
    (contract:<runbook path>#<task> for the task layer, contract:<runbook path>#- for a pack
    edit), accepted and new JSON values the start verb writes with util.canonical_json into the
    loosen hold's text and grant command (operator-surface sections 4.3.1 and 4.7). Empty for
    every other code."""

    def __init__(self, code: int, reason: str, detail: str = "", *, loosened=()):
        super().__init__(code, reason, detail)
        self.loosened = tuple(loosened)


class IsolationError(ResearchError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__("%s: %s" % (reason, detail))
        self.reason, self.detail = reason, detail


class LaunchError(ResearchError):
    """A launch that could not be prepared (not an agent failure, which is a LaunchResult)."""


class GateInfraError(ResearchError):
    """A gate that could not run at all."""


class MarkerError(ResearchError):
    """A test or metric log without its boundary markers, or with a conflicting result."""


class LockTimeout(ResearchError):
    """A machine lock not taken within its timeout."""


# reason code -> exit code
CODES = {
    # runner.open_run and the runner's own preflight (t-100)
    "RUN-RUNTIME-MISSING": vc.BLOCKED,
    "RUN-JAIL-UNAVAILABLE": vc.BLOCKED,
    "RUN-DISK-LOW": vc.BLOCKED,
    "RUN-CLONE-FAILED": vc.BLOCKED,
    "RUN-BUDGET-HOLD": vc.PAUSED,               # also the code of a budget hold (holds.HOLD_CODES)
    "RUN-ROUNDS-REACHED": vc.PAUSED,            # the extension hold at the bound (section 4.3)
    "RUN-SHIP-DECISION": vc.PAUSED,             # the ship hold (section 4.4)
    "RUN-PLAN-CAP": vc.FAIL,
    "RUN-STORE-TRACKED": vc.FAIL,
    "RUN-CONTRACT-CHANGED": vc.FAIL,
    "RUN-BASELINE-FAILED": vc.FAIL,
    # a second runner exits with operator-surface's RESEARCH-RUN-ACTIVE (below, section 4.2)
    # the contract loader (t-096), runbook-format-3.md section 6.2, every code of its table
    "CONTRACT-CHECK-FAILED": vc.FAIL,
    "CONTRACT-CHECK-BLOCKED": vc.BLOCKED,
    "CONTRACT-TASK-UNKNOWN": vc.FAIL,
    "CONTRACT-REPO-UNREADABLE": vc.BLOCKED,
    "CONTRACT-PACK-OUTSIDE-REPO": vc.FAIL,
    "CONTRACT-REPRODUCER-MISSING": vc.BLOCKED,
    "CONTRACT-OVERRIDE-INVALID": vc.FAIL,
    "CONTRACT-LOOSEN-UNGRANTED": vc.PAUSED,
    "CONTRACT-ALLOWLIST-OVERLAP": vc.FAIL,
    "CONTRACT-UNPROTECTED": vc.FAIL,
    "CONTRACT-EDIT-DURING-RUN": vc.FAIL,        # the code of a research-contract-drift event
    # isolation (t-091), recorded in events, never an exit on their own
    "ISOLATION-PATH-TAKEN": vc.BLOCKED,
    "ISOLATION-CONTAINMENT": vc.BLOCKED,
    "ISOLATION-BARRIER": vc.BLOCKED,
    "ISOLATION-CONFIG": vc.BLOCKED,             # a clone config that would let git read an agent's
                                                # config file (section 3.1)
    "ISOLATION-REGISTRY": vc.BLOCKED,           # a clone whose .git/worktrees/ is not an empty
                                                # folder: a registered worktree (section 3.1)
    "ISOLATION-REBUILD-MISMATCH": vc.FAIL,
    # gates (t-093)
    "GATE-LOCK-TIMEOUT": vc.BLOCKED,
    "GATE-MARKERS": vc.FAIL,
    "GATE-EVIDENCE-LINK": vc.BLOCKED,           # gates.judged_copy refused a symlink, a special
                                                # file or an entry outside the evidence folder
    # repair (t-101): the base pass set is smaller than pass_to_pass.min_tests; the prepare stage
    # did not PASS; the reproducer did not fail on the base or its review did not pass (7.4)
    "REPAIR-BASELINE-THIN": vc.FAIL,
    "REPAIR-PREPARE-FAILED": vc.FAIL,
    "REPAIR-REPRODUCER-UNPROVED": vc.FAIL,
    # the verbs (t-105), operator-surface.md section 4.4, every code of its table
    "RESEARCH-RUN-UNKNOWN": vc.FAIL,
    "RESEARCH-RUN-FINISHED": vc.FAIL,
    "RESEARCH-RUN-ACTIVE": vc.FAIL,
    "RESEARCH-RUN-NOT-HALTED": vc.FAIL,
    "RESEARCH-HOLD-OPEN": vc.PAUSED,
    "RESEARCH-HOLD-NONE": vc.FAIL,
    "RESEARCH-OWNER-GRANT-MISSING": vc.PAUSED,
    "RESEARCH-OWNER-NOT-TERMINAL": vc.FAIL,
    "RESEARCH-OWNER-CONFIRM-MISMATCH": vc.FAIL,
    "RESEARCH-GRANT-TERMS": vc.FAIL,
    "RESEARCH-RUNBOOK-STOPPED": vc.PAUSED,
    "RESEARCH-CONTRACT-BUSY": vc.FAIL,
    "RESEARCH-CONTROL-REQUIRED": vc.FAIL,
    "RESEARCH-CONTROL-FAILED": vc.FAIL,
    "RESEARCH-NOT-FORMAT-3": vc.FAIL,
    "RESEARCH-TEMPLATE-EXISTS": vc.FAIL,
    "RESEARCH-LEVEL-UNREADABLE": vc.BLOCKED,
    "RESEARCH-RECORD-UNREADABLE": vc.BLOCKED,
    # the session runtime (in-session-runtime.md section 3.3): the job protocol, the start
    # checks of a session run and the guard against a headless agent
    "JOB-UNKNOWN": vc.FAIL,
    "JOB-NOT-DRIVER": vc.FAIL,
    "JOB-IN-SUBAGENT": vc.BLOCKED,
    "JOB-STATE": vc.FAIL,
    "JOB-NONCE": vc.FAIL,
    "JOB-DEADLINE": vc.FAIL,
    "JOB-PENDING": vc.BLOCKED,
    "JOB-NO-STOP": vc.BLOCKED,
    "JOB-STOP-MISMATCH": vc.FAIL,
    "JOB-AGENT-TYPE": vc.FAIL,
    "JOB-NO-TRANSCRIPT": vc.BLOCKED,
    "JOB-TRANSCRIPT-CHANGED": vc.FAIL,
    "JOB-TRANSCRIPT-TOO-LARGE": vc.FAIL,
    "JOB-AGENT-REUSED": vc.FAIL,
    "JOB-DUPLICATE-SPAWN": vc.FAIL,
    "JOB-SESSION-MISMATCH": vc.FAIL,
    "JOB-TRANSCRIPT-FRAME": vc.FAIL,
    "JOB-PROMPT-UNREAD": vc.FAIL,
    "JOB-RELAY": vc.FAIL,
    "JOB-HOOK-CONTEXT": vc.FAIL,
    "JOB-CONFIG-CHANGED": vc.FAIL,
    "JOB-ENGINE-CHANGED": vc.FAIL,
    "JOB-CONTEXT-CHANGED": vc.FAIL,
    "JOB-CONTEXT-UNLISTED": vc.FAIL,
    "JOB-TOOL-OFF-LIST": vc.FAIL,
    "JOB-BACKGROUND": vc.FAIL,
    "JOB-MODEL-MISMATCH": vc.FAIL,
    "JOB-FINAL-MISSING": vc.FAIL,
    "JOB-FINAL-MISMATCH": vc.FAIL,
    "JOB-POOL-MISMATCH": vc.FAIL,
    "JOB-SCOPE-BREACH": vc.FAIL,
    "JOB-STRAGGLER": vc.FAIL,
    "JOB-RECORD-INTRUSION": vc.FAIL,
    "JOB-SIBLING-REF": vc.FAIL,
    "JOB-GROUP-ORDER": vc.FAIL,
    "JOB-PROMPT-TOO-LARGE": vc.FAIL,
    # submit of one member of a verifier group whose sibling has no claim yet: the claim stays
    # filed, the judge waits for the sibling, and the same call is repeated (fix batch 3, F18)
    "JOB-GROUP-WAIT": vc.BLOCKED,
    "RESEARCH-LEVEL-TOO-LOW": vc.BLOCKED,
    "RUN-DRIVER-IS-SUBAGENT": vc.BLOCKED,
    "RUN-DRIVER-GONE": vc.BLOCKED,
    "RUN-ENGINE-CHANGED": vc.BLOCKED,
    "RUN-RUNTIME-UNMAPPED": vc.BLOCKED,
    "RUN-NO-TRANSCRIPT": vc.BLOCKED,
    "RUN-ISOLATION-MISSING": vc.BLOCKED,
    "RUN-RATECARD-MISSING": vc.BLOCKED,
    "RUN-CODEX-NOT-READY": vc.BLOCKED,
    "RUN-HEADLESS-REFUSED": vc.BLOCKED,
    # the run-level model override (start --model, in-session-runtime.md sections 3.4 and 4.3)
    "RUN-MODEL-UNKNOWN": vc.FAIL,               # --model names no alias the shield policy maps
    "RUN-MODEL-MISMATCH": vc.FAIL,              # resume or extend --model differs from the run's
}


def refusal(reason: str, detail: str = "") -> ResearchRefusal:
    """The refusal for a known reason code, with its exit code from CODES."""
    return ResearchRefusal(CODES[reason], reason, detail)
