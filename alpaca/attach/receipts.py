"""The three receipts of a handoff (op-062 t-686, build plan P15).

Each is attach's own dataclass with its own `kind`, recorded through `alpaca.release.receipts.record_receipt`, so the
event kind is `release-attach-approval`, `release-attach-handoff` or `release-attach-merge`. `release.approval.current`
reads `release-approval` only: an attach approval never authorizes a release publication, and a release approval
never authorizes a handoff (the two kinds do not meet). `release.receipts.load_receipt` loads each by its `kind`
and checks the file against the hash the event holds, so an edited receipt fails closed.

The dataclasses hold plain values only (strings, lists, dicts of strings), so the canonical JSON of a receipt is
the same on every machine.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from alpaca.release.receipts import timestamp


@dataclass
class AttachApprovalReceipt:
    """Who approved or revoked one exact head commit of one task, and what the approval is bound to: the
    repository (owner/name), the upstream URL, the work branch, the base branch and the digest of the gate set.
    A change to any of them makes the approval void (the approval layer compares each one again at use)."""
    source_sha: str
    approver: str
    decision: str
    task: str
    repository: str
    url: str
    branch: str
    base: str
    gate_set: str
    delegation: str = ''
    delegation_sha256: str = ''
    dec_id: str = ''
    actions: list = field(default_factory=list)
    status: str = ''
    #: also bound (op-062 t-690, t-689): whether the pull request body carries the generated section, and the commit
    #: identity. A receipt written before they existed holds the defaults, which is what such a studio did.
    pr_footer: bool = True
    commit_identity: str = 'Alpaca agent <alpaca-agent@localhost>'
    attempt: str = field(default_factory=lambda: uuid.uuid4().hex)
    kind: str = 'attach-approval'
    schema: int = 1
    created_at: str = field(default_factory=timestamp)


@dataclass
class AttachHandoffReceipt:
    """One handoff: a dry run (`DRY_RUN`) or an executed push. `outgoing` is the whole plan (repository, url,
    branch, head, base, commits, files, gates, titles and the body digest); an executed handoff must find the
    recomputed plan equal to the reviewed dry run. Statuses: DRY_RUN, PUSHED (branch pushed and pull request
    open), PUSHED_NO_PR (branch pushed, the pull request step failed; a retry opens it), PUSHED_UNVERIFIED (branch
    pushed, but the push report or the upstream's refs afterwards were not exactly the approved branch; no pull request
    is opened and `error` says what differed)."""
    task: str
    outgoing: dict
    status: str
    pr_url: str = ''
    pr_number: int = 0
    error: str = ''
    note: str = ''
    attempt: str = field(default_factory=lambda: uuid.uuid4().hex)
    kind: str = 'attach-handoff'
    schema: int = 1
    created_at: str = field(default_factory=timestamp)


@dataclass
class AttachMergeReceipt:
    """A pull request merged by the agent under a merge delegation, with the head sha pinned in the call."""
    task: str
    head: str
    pr_number: int
    merge_sha: str
    method: str
    status: str
    error: str = ''
    attempt: str = field(default_factory=lambda: uuid.uuid4().hex)
    kind: str = 'attach-merge'
    schema: int = 1
    created_at: str = field(default_factory=timestamp)
