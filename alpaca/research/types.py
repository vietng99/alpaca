"""Shared data types of the research engine. Pure data: no I/O and no import from another
alpaca.research module. Design: design/research-engine/architecture.md section 12.2."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Literal, Optional, TypedDict

Mode = Literal["improve", "repair"]
RuntimeName = Literal["claude", "codex", "control"]
Direction = Literal["higher", "lower"]
MetricKind = Literal["deterministic", "noisy"]
AcceptRule = Literal["champion", "deficit-pareto", "repair"]           # research.accept.rule
AfterTargets = Literal["stop", "pareto"]                                # research.accept.after_targets
GateRole = Literal["stage", "metric", "fail-to-pass", "pass-to-pass"]   # a chain item's role (gates.chain)
FieldType = Literal["str", "text", "int", "float", "bool", "enum", "list", "paths"]   # proposal[].type

ROLES = ("proposer", "implementer", "notes", "auditor", "verifier",
         "reproducer-writer", "reproducer-reviewer")      # launch labels; t-089 owns the last two's use
WRITING_ROLES = ("implementer", "reproducer-writer")
TASK_ROLES = ("attempt", "repair", "report")              # extra.research.role (t-089's names)
RUN_STATES = ("starting", "running", "halted", "paused", "resumable", "stopping", "stopped",
              "interrupted", "finished", "failed")
ATTEMPT_STATES = ("planned", "claimed", "window-open", "proposing", "proposed",
                  "proposal-checked", "isolated", "implementing", "implemented", "checked",
                  "captured", "purged", "gated", "judged", "audited", "noted", "window-closed",
                  "recorded", "torn-down", "sealed", "verifying", "verified", "refuted",
                  "unverified", "merged", "done", "blocked")
TERMINAL_STATES = ("done", "blocked")
VERDICTS = ("accepted", "rejected", "broken", "abandoned", "baseline")
OUTCOMES = ("baseline", "improvement", "fixed", "regression", "repeats-failed", "untested",
            "not-fixed", "gate-failed", "broken", "abandoned")
VERDICT_OF = {"baseline": "baseline", "improvement": "accepted", "fixed": "accepted",
              "regression": "rejected", "repeats-failed": "rejected", "untested": "rejected",
              "not-fixed": "rejected", "gate-failed": "rejected", "broken": "broken",
              "abandoned": "abandoned"}
BROKEN_CLASSES = ("schema", "tamper", "allowlist", "purge", "gate-blocked", "timeout", "diff",
                  "audit", "audit-tamper")
INFRA_CLASSES = ("start-failed", "quota", "auth", "crash", "lost", "timeout")
ABANDON_REASONS = INFRA_CLASSES + ("audit-blocked", "stopped")
VERIFY_OUTCOMES = ("verified", "refuted", "unverified", "superseded")   # t-089's
LAUNCH_OUTCOMES = ("ok", "timeout", "start-failed", "quota", "auth", "crash", "killed", "held",
                   "lost")   # "lost": a launch whose prior try was found on resume with no end; it
                             # records research-infra, not research-launch-end (section 6.1, step 1)
                             # "held": the budget check said HOLD; nothing is recorded (step 2)
JAIL_KINDS = ("agent", "gate", "holdout", "probe")
JOB_STATES = ("issued", "handed", "claimed", "accepted", "refused", "expired")   # the states of a job
                             # of the session runtime (in-session-runtime.md section 4): open until it
                             # is accepted, refused for good or expired
# t-088's role keys of research.runtime.timeouts and .effort -> the launch label of ROLES
AGENT_ROLE_KEYS = {"proposer": "proposer", "implementer": "implementer", "notes": "notes",
                   "audit": "auditor", "reproducer": "reproducer-writer",
                   "reviewer": "reproducer-reviewer"}


def to_dict(obj) -> dict:
    """A dataclass as plain data for an event's `data`."""
    return dataclasses.asdict(obj)


@dataclass(frozen=True)
class Metric:
    """research.metrics[] (t-088 section 4)."""
    id: str
    command: str            # the measuring command, run by `sh -c` in the gate jail once per repeat
    parser: str             # "markers" or "json-field"
    unit: str
    direction: Direction
    kind: MetricKind
    repeats: int            # engine default 1 when deterministic
    min_ok: int
    margin: float
    timeout_s: int          # .timeout, seconds per repeat (required)
    median: str = "worse"   # "worse" or "middle", for an even count of repeats
    seeds: tuple = ()       # one int per repeat, given to the command as ${SEED}
    workdir: Optional[str] = None   # a path in the runbook folder; None: the tree's copy of it
    path: Optional[str] = None      # json-field: the JSON file the command writes
    field: Optional[str] = None     # json-field: the dotted path of the number in it
    target: Optional[float] = None  # owner-only (FR-008); also listed in Contract.targets
    artifact: Optional[str] = None
    description: Optional[str] = None


@dataclass(frozen=True)
class Target:
    metric: str
    value: float


@dataclass(frozen=True)
class GateStage:
    """research.gates[]: a build or check stage of the runbook, run in every attempt."""
    stage: str              # a runbook stage id, read within its runbook
    timeout_s: int          # the stage's own `timeout` (stages[].timeout)
    heavy: bool = False
    lock_timeout_s: int = 14400     # .lock_timeout


@dataclass(frozen=True)
class ProposalField:
    """research.proposal[]: a pack field beyond the base fields (t-088 section 5)."""
    field: str
    type: FieldType
    required: bool = True
    values: tuple = ()              # enum only
    max: Optional[int] = None       # most characters or items; None: the engine default of the type
    description: Optional[str] = None


@dataclass(frozen=True)
class Category:
    """research.categories[]: slot s gets category s modulo their count (FR-043)."""
    id: str
    text: str


@dataclass(frozen=True)
class AgentSettings:
    """research.runtime.timeouts.<role> and .effort.<role>, keyed by launch label."""
    role: str                       # a launch label of ROLES (AGENT_ROLE_KEYS maps t-088's keys)
    timeout_s: Optional[int] = None
    effort: Optional[str] = None    # "low", "medium" or "high"; None: the runtime's own


@dataclass(frozen=True)
class FailToPassSpec:
    """research.repair.cases[].fail_to_pass."""
    origin: str                     # "store" or "generate"
    command: str = "sh ${REPRODUCER_DIR}/run.sh"
    tests: tuple = ()               # TEST names; for origin generate, frozen with the proof
    timeout_s: int = 1200
    repeats: int = 1


@dataclass(frozen=True)
class PassToPassSpec:
    """research.repair.pass_to_pass, one per runbook."""
    command: str                    # run from ${CONTRACT_DIR}
    min_tests: int
    timeout_s: int


@dataclass(frozen=True)
class RepairItem:
    """research.repair.cases[]; index is its place in the list, the i of <run>:repair:<i>."""
    index: int
    case_id: str                    # .id: names contract/hidden/<case id>/ and the store folder
    bug_report: str                 # a path in the runbook folder
    fail_to_pass: FailToPassSpec
    base: Optional[str] = None      # 40 hex digits; None: the run's base commit
    suspect_files: tuple = ()
    fix_terms: tuple = ()           # fix-terms.txt of the case's store folder, read at freeze


@dataclass(frozen=True)
class CoupledGroup:
    """research.coupled[]: files that must change together (t-088 section 14.5)."""
    id: str
    files: tuple
    note: Optional[str] = None


@dataclass(frozen=True)
class Contract:
    """The frozen research contract of one run: t-088's contract object (runbook-format-3.md
    section 14.6). Built only by contract.from_block. Each comment names the YAML path of t-088's
    field table (section 4); `block` holds the whole merged block."""
    runbook: str                    # the runbook path
    runbook_id: str
    runbook_sha256: str
    pack: Optional[str]             # the defaults file's `pack`
    task: Optional[str]             # the task whose override was merged, or None
    ref: str                        # contract:<runbook path>#<task id or ->
    mode: Mode                      # research.mode
    repo_path: str                  # research.repo.path (engine: ".")
    repo_ref: str                   # research.repo.ref (engine: "HEAD")
    repo_commit: str                # the base commit repo_ref resolved to at freeze
    stages: tuple                   # the runbook's stage mappings, as frozen
    knobs: dict                     # the runbook's knobs, as frozen
    metrics: tuple                  # of Metric: research.metrics[] (improve)
    targets: tuple                  # of Target: each metrics[].target
    accept_rule: AcceptRule         # research.accept.rule
    after_targets: Optional[AfterTargets]   # research.accept.after_targets (engine: "stop";
                                    # DD-01); None for a block with no target (t-088 section 7.2)
    editable: tuple                 # research.paths.editable
    protected: tuple                # research.paths.protected
    hidden: tuple                   # research.paths.hidden
    tests: tuple                    # research.paths.tests
    read_paths: tuple               # research.paths.read
    read_real: tuple                # realpath of each read path, frozen at the run start
    keep: tuple                     # research.paths.keep
    outputs: tuple                  # research.paths.outputs: ignored outputs the eval may create
    tools: tuple                    # research.fingerprint.tools
    tools_real: tuple               # realpath of each tool on the tool PATH, frozen at the run
                                    # start and refused in a forbidden path (section 3.5)
    shared: tuple                   # research.fingerprint.shared
    gates: tuple                    # of GateStage: research.gates[], in chain order
    slots: int                      # K: research.stop.slots (per open case in repair mode)
    rounds: int                     # N: research.stop.rounds
    budget_usd: Optional[float]     # research.budget.usd; None: no dollar cap (t-297)
    budget_tokens: Optional[int]    # research.budget.tokens; None: no token cap (t-297)
    budget_wall_s: Optional[int]    # research.budget.wall; None: no wall-time cap
    disk_gib: float                 # research.disk
    proposal: tuple                 # of ProposalField: research.proposal[]
    categories: tuple               # of Category: research.categories[]
    lessons_file: Optional[str]     # research.lessons.file
    note_chars: int                 # research.lessons.note_chars (engine: texts.NOTE_MAX_CHARS)
    noop_rounds: int                # research.controls.noop_rounds
    controls_random: bool           # research.controls.random
    controls_mutate: Optional[str]  # research.controls.mutate: a script in the runbook folder
    controls_syntax: Optional[str]  # research.controls.syntax: a gate stage id
    controls_seed: int              # research.controls.seed (engine: 0)
    holdout_stages: tuple           # research.holdout.stages
    holdout_paths: tuple            # research.holdout.paths
    repair: tuple                   # of RepairItem: research.repair.cases[]
    pass_to_pass: Optional[PassToPassSpec]   # research.repair.pass_to_pass
    repair_prepare: Optional[str]   # research.repair.prepare: a stage id
    coupled: tuple                  # of CoupledGroup: research.coupled[]
    agents: tuple                   # of AgentSettings: research.runtime.timeouts and .effort
    infra_retries: int              # research.runtime.retries (engine: 2)
    verifier_timeout_s: int         # research.verifier.timeout (engine: 1800)
    verifier_rerun_timeout_s: int   # research.verifier.rerun_timeout (engine: 1200)
    audit_hints: Optional[str]      # research.audit.hints
    audit_assert_tokens: tuple      # research.audit.assert_tokens
    audit_build_files: tuple        # research.audit.build_files
    audit_steering: tuple           # research.audit.steering
    egress_extra: tuple             # research.egress.allow
    block: dict                     # the frozen merged block, whole (section 11.1)
    override_diff: tuple            # of OverrideDiff (t-088 section 7.6)
    grants: tuple                   # decision ids of the loosen grants used
    reproducers: dict               # origin store case id -> its contract/hidden/<case id>/
    pack_sha256: str
    sha256: str                     # contract_sha256; "" until freeze fills it
    files_sha256: str               # "" until freeze fills it (dataclasses.replace)


@dataclass(frozen=True)
class OverrideDiff:
    """One entry of t-088's recorded diff (runbook-format-3.md section 7.6); contract.py writes
    field_class as the key `class` in contract.json and the run-start event."""
    field: str                      # the leaf field path, e.g. research.metrics[compares].target
    pack: Any
    task: Any
    field_class: str                # t-088's override class of section 7.5
    direction: str                  # contract.classify's word: "tightened", "loosened", "free", "added"
    grant: Optional[str] = None     # the decision id that allowed a loosening


@dataclass(frozen=True)
class CloneInfo:
    path: str
    line: str                       # "champion" or a repair task id
    branch: str
    base_commit: str
    head: str


@dataclass(frozen=True)
class WorktreeInfo:
    path: str
    label: str                      # an attempt task id, or r<N>-champion
    commit: str
    admin_dir: Optional[str]        # the admin folder the engine made outside the clone's .git
                                    # (section 3.1), recorded at cut time and never read again
                                    # from `.git`; None for a tree cut with admin False
    private_objects: Optional[str] = None  # the agent's store (attempt worktrees only)
    index: Optional[str] = None     # the engine's index file, <host_git>/index (section 3.1)
    host_git: Optional[str] = None  # the engine's GIT_DIR for host git: HEAD, index, objects/
    read_only: bool = False


@dataclass(frozen=True)
class PurgeResult:
    phase: str                      # "pre-eval" or "post-eval"
    removed: tuple = ()
    kept: tuple = ()
    new: tuple = ()                 # post-eval: new ignored paths outside keep and eval outputs
    changed: tuple = ()             # post-eval: tracked or untracked paths that differ from head
    nested: tuple = ()              # post-eval: `.git` entries below the root (isolation.nested_git)
    ok: bool = True


@dataclass(frozen=True)
class DiffCapture:
    forked_from: str
    head: str                       # forked_from when empty
    tree: str
    path: str                       # attempt.diff, a zero-byte file when empty
    sha256: str
    bytes: int
    paths: tuple
    stamp: str
    message: str
    empty: bool = False             # no path changed: no commit made (section 3.1, capture)
    gitlinks: tuple = ()            # mode 160000 paths `git add -A` staged (a nested repository)


@dataclass(frozen=True)
class ToolPin:
    name: str
    resolved: Optional[str]
    sha256: Optional[str]
    mode: Optional[int]
    error: Optional[str] = None


@dataclass(frozen=True)
class FingerprintPoint:
    run: str
    task: str
    point: str
    taken_at: str
    entries: dict                   # source key -> digest
    errors: tuple
    path: str                       # the saved json


@dataclass(frozen=True)
class TamperVerdict:
    point: str
    ok: bool
    changed: tuple = ()
    missing: tuple = ()
    outside: tuple = ()
    protected: tuple = ()
    errors: tuple = ()
    affected: tuple = ()            # other attempt tasks flagged by a shared change
    nested: tuple = ()              # nested repositories: check_paths' walk, check_diff's gitlinks


@dataclass(frozen=True)
class TestReport:
    tests: dict                     # name -> "PASS", "FAIL" or "SKIP"
    begin: bool
    end: bool
    conflicts: tuple = ()


@dataclass(frozen=True)
class MetricSample:
    metric: str
    repeat: int
    value: Optional[float]
    ok: bool
    log: str


@dataclass(frozen=True)
class MetricValue:
    metric: str
    median: Optional[float]
    repeats: tuple                  # of Optional[float], every repeat in order
    n_ok: int
    spread: Optional[float]         # max minus min of the successful repeats (DD-11)
    untested: bool = False


@dataclass(frozen=True)
class GateResult:
    stage: str
    side: str                       # "attempt", "base" or "verify"
    verdict: int                    # an alpaca.gates.verdict code
    rc: Optional[int]
    timed_out: bool
    log: str
    input_sha256: str
    offline: Optional[bool]
    lock_wait_s: float = 0.0
    reused: bool = False
    samples: tuple = ()             # of MetricSample
    tests: Optional[TestReport] = None
    event_id: Optional[int] = None


@dataclass(frozen=True)
class FailToPass:
    tests: tuple                    # the reproducer's TEST names
    base: dict                      # test name -> "PASS", "FAIL" or "SKIP" on the base tree
    attempt: dict                   # test name -> result on the attempt tree
    weak: tuple                     # tests that do not FAIL on the base (refused as weak)
    ok: bool                        # every test FAIL on the base and PASS on the attempt, none weak
    base_event: Optional[int] = None      # the research-gate event of each side
    attempt_event: Optional[int] = None


@dataclass(frozen=True)
class PassToPass:
    baseline: tuple                 # the tests that must keep passing (the repair task's base set)
    base_pass: tuple                # passing on the base tree now
    attempt_pass: tuple             # passing on the attempt tree
    lost: tuple                     # in baseline and not in attempt_pass
    ok: bool                        # lost is empty
    base_event: Optional[int] = None
    attempt_event: Optional[int] = None


@dataclass(frozen=True)
class ChainResult:
    verdict: int
    results: tuple                  # of GateResult
    first_failure: Optional[str]
    metrics: dict                   # metric id -> MetricValue
    fail_to_pass: Optional[FailToPass] = None
    pass_to_pass: Optional[PassToPass] = None


@dataclass(frozen=True)
class AcceptDecision:
    accepted: bool
    outcome: str                    # one of OUTCOMES
    phase: str                      # "strict", "deficit", "pareto", "repair" or "baseline"
    reason: str
    score: Optional[float] = None
    champion_score: Optional[float] = None
    untested: bool = False
    repeats_failed: bool = False
    margin: float = 0.0


@dataclass(frozen=True)
class Candidate:
    task: str
    round: int
    slot: int
    metrics: dict                   # metric id -> MetricValue
    decision: AcceptDecision


@dataclass(frozen=True)
class Ranked:
    task: str
    rank: int
    key: tuple
    reason: str


@dataclass(frozen=True)
class Proposal:
    attempt: str
    category: str
    fields: dict
    sha256: str
    path: str


@dataclass(frozen=True)
class JailSpec:
    """Every kind gets --unshare-net; an agent jail reaches its provider only through egress."""
    kind: str                       # one of JAIL_KINDS
    cwd: str
    ro: tuple = ()
    rw: tuple = ()
    git_hide: tuple = ()            # clone .git folders whose (empty) worktrees/ gets a tmpfs
    admins: tuple = ()              # of (admin folder, writable), bound after the tmpfs
    overlays: tuple = ()            # of (source, destination), read-only, after the rw binds:
                                    # frozen copies in a gate jail, the credential file in an agent jail
    egress: Optional[str] = None    # the egress socket folder, bound at /run/alpaca-egress


@dataclass(frozen=True)
class ProcResult:
    rc: Optional[int]
    timed_out: bool
    stopped: bool
    killed: tuple                   # pids killed at cleanup
    log: str
    started: str
    ended: str
    duration_s: float


@dataclass(frozen=True)
class LaunchSpec:
    """The keys of the mapping launcher.launch takes; t-089's keys come first (verifier.md
    section 12)."""
    role: str                       # one of ROLES, the launch label
    runtime: str                    # one of RuntimeName
    session: str                    # minted by the caller (section 4.1; verifier.md section 5.5)
    workdir: str                    # the folder the launch works in
    cwd: str
    prompt: str
    timeout_s: int
    key: str                        # the resume key (section 6.1)
    run: Optional[str] = None       # None for a verification outside a run
    attempt: Optional[str] = None   # the task the launch serves
    read_paths: tuple = ()
    write_paths: tuple = ()
    tools: tuple = ()               # the role's tools, from its archetype
    op: Optional[str] = None        # the run's op, for dispatch.spawn and the event
    worker: Optional[str] = None    # the dispatch worker id, built by the lifecycle
    try_n: int = 1
    round: Optional[int] = None
    slot: Optional[int] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    max_usd: Optional[float] = None # what is left of the run's dollar cap (section 4.6)
    output_schema: Optional[dict] = None
    git_objects: Optional[str] = None      # GIT_OBJECT_DIRECTORY: the attempt's private store
    git_alternates: Optional[str] = None   # GIT_ALTERNATE_OBJECT_DIRECTORIES: the clone's objects
    control: Optional[str] = None   # "noop" or "random" for runtime "control"
    control_seed: Optional[int] = None
    control_mutate: Optional[str] = None   # the frozen controls.mutate script, for "random"
    evidence_dir: Optional[str] = None     # where out/ moves after the launch
    binary: Optional[str] = None    # t-089's pin: the runtime file to run exactly (section 6.1)
    binary_sha256: Optional[str] = None    # required with binary; a mismatch refuses the launch
    bash_allow: tuple = ()          # t-089's SHELL_ALLOW: Claude Bash(<cmd>:*) rules (section 6.2)
    git_admin: Optional[str] = None # WorktreeInfo.admin_dir of the launch's one tree; None:
                                    # no .git in the jail (verifier, auditor, notes writer)
    git_common: Optional[str] = None  # that tree's clone .git (CloneInfo.path + "/.git"); set
                                    # exactly when git_admin is (section 6.1, step 3)
    group: Optional[str] = None     # a verifier pair's independence group "<vf>/r<round>"; the
                                    # session runtime hands both jobs of a group out together
    group_size: Optional[int] = None       # the jobs of that group in this batch (2, or 1)
    first_pass: Optional[bool] = None      # whether the verifier slot is in its first pass
    verifier_slot: Optional[str] = None    # "v1" or "v2" for a verifier launch
    # git_hide and admins are not spec keys: launcher.jail_spec builds them from git_admin and
    # git_common


class Job(TypedDict, total=False):
    """job.json of the session runtime (in-session-runtime.md section 4.1): one agent launch, as
    a record. schema is "alpaca-research-job/1"."""
    schema: str
    job: str
    scope: str
    run: Optional[str]
    op: Optional[str]
    attempt: Optional[str]
    role: str
    slot: Optional[int]
    verifier_slot: Optional[str]
    round: Optional[int]
    try_n: int
    key: str
    runtime: str
    tier: str
    model: Optional[str]
    effort: Optional[str]
    nonce: str
    prompt: str
    prompt_path: str
    prompt_sha256: str
    spawn_prompt: str
    spawn_prompt_sha256: str
    cwd: str
    workdir: str
    shell_cwd: str
    read_paths: list
    write_paths: list
    allowed_tools: list
    bash_allow: list
    scanned_by: str
    timeout_s: int
    issued_at: str
    handoff_by: str
    deadline: Optional[str]
    config_sha256: str
    budget_hint_usd: Optional[float]
    group: Optional[str]
    group_size: Optional[int]
    first_pass: Optional[bool]
    description: str


class JobResult(TypedDict, total=False):
    """The result the judge records for an accepted job (in-session-runtime.md section 4.2):
    the driver's claim plus what the judge measured."""
    job: str
    nonce: str
    agent_id: str
    transcript: Optional[str]
    final: Optional[str]
    usage: Optional[dict]
    tool: Optional[str]
    tool_use_id: Optional[str]
    launch_end: int
    transcript_sha256: str
    source_transcript: str
    source_sha256: str
    final_sha256: str
    measured_usage: dict
    outcome: str
    models_seen: list
    tools_seen: list
    runtime_version: Optional[str]
    started: Optional[str]
    ended: Optional[str]
    checks: list


@dataclass(frozen=True)
class LaunchHome:
    launch_id: str
    root: str
    home: str
    config: str                     # CLAUDE_CONFIG_DIR or CODEX_HOME
    out: str
    credential_file: Optional[str] = None


@dataclass(frozen=True)
class LaunchResult:
    """The keys of the mapping launcher.launch returns; t-089's keys come first."""
    session: str
    rc: Optional[int]
    timed_out: bool
    held: bool                      # True when the launch was held (spend.check HOLD on a run cap)
    launch_id: str
    key: str
    role: str
    try_n: int
    outcome: str                    # one of LAUNCH_OUTCOMES
    # The next four default, and follow the fields without one (a dataclass needs that order;
    # the mapping's keys are the same): a held result (section 6.1, step 2) and a lost one
    # (step 1) return before prepare_home, so they have no home, stream or duration yet.
    home: str = ""                  # the launch home (runtime state and transcript folder)
    duration_s: float = 0.0
    stream: str = ""
    stream_sha256: Optional[str] = None
    launch_event: Optional[int] = None
    launch_end: Optional[int] = None       # the research-launch-end event id (verifier.md section 12)
    infra: Optional[str] = None     # the infrastructure class of a failed launch, else None
    reason: str = ""                # why, for a failure or a hold
    transcript: Optional[str] = None
    transcript_sha256: Optional[str] = None
    attempt: Optional[str] = None
    final_message: Optional[str] = None
    egress_log: Optional[str] = None
    egress_sha256: Optional[str] = None
    tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    priced: bool = False
    reset_at: Optional[str] = None
    killed: tuple = ()


@dataclass(frozen=True)
class SpendCheck:
    posture: str                    # "GO" or "HOLD": a run cap or unpriced spend under one (t-297)
    reason: str
    usd_spent: float
    tokens_spent: int
    usd_cap: Optional[float]        # newest terminal-confirmed budget grant's usd, else budget_usd (3.8)
    tokens_cap: Optional[int]       # newest terminal-confirmed budget grant's tokens, else budget_tokens
    wall_s_spent: float             # seconds the run's runner processes have been alive (3.8)
    wall_cap_s: Optional[int]       # newest terminal-confirmed budget grant's wall, else budget_wall_s
    unpriced: int
    project_posture: str


@dataclass
class RunContext:
    """What every lifecycle step receives. Not frozen: it carries live objects."""
    root: str
    run: str
    op: str
    session: str                    # research-<run>
    contract: Contract
    runtime: str
    clone: Optional[CloneInfo] = None
    stop: Any = None                # a threading.Event set by the signal handler
    barrier: Any = None             # the round barrier of section 4.3
    tmp_prefixes: Optional[tuple] = None   # passed to workspace_guard in tests
