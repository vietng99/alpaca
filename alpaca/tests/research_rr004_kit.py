"""The rr-004 fixtures of alpaca/tests/test_research_calltime.py (in-session-runtime.md section 18).

The control run rr-004 lost six of nine verifier passes to the form of a call or of a piece of
evidence. The fixtures hold the records of the calls that did it, cut from the raw subagent
transcripts (alpaca/tests/fixtures/research/rr004/MANIFEST.json names each source and the sha256
of the raw file; build_fixtures.py rebuilds them). Four things differ from the source bytes, all
about what a shipped file may hold: the project root and the home folder are the placeholders
`@ROOT@` and `@HOME@`, which the loaders here replace by the test project's root and a test home;
every sealed term and every e-mail address is `SEALEDTERM` or `EMAILADDR`; the runtime's static
context records of the whole transcripts are cut to a stub; and every file holds only ASCII.
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import shutil

from alpaca.research import launcher

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "research", "rr004")

#: the seven transcripts the replay reads: agent id -> (job, try folder under .alpaca/verify, slot)
AGENTS = {
    "a84427dcadd1ac87b": ("vf-t-193-82943-v2-r0-t0", "t-193/vf-t-193-82943/v2/r0/t0", "v2"),
    "aa63cf2e2ead94ce0": ("vf-t-194-83138-v1-r0-t0", "t-194/vf-t-194-83138/v1/r0/t0", "v1"),
    "a05179e7708318396": ("vf-t-193-82943-v1-r0-t2", "t-193/vf-t-193-82943/v1/r0/t2", "v1"),
    "af8d86438e38dbbd8": ("vf-t-194-83138-v1-r0-t2", "t-194/vf-t-194-83138/v1/r0/t2", "v1"),
    "a660f66772906b0c2": ("vf-t-194-83138-v1-r0-t1", "t-194/vf-t-194-83138/v1/r0/t1", "v1"),
    "ad36c863082a96f89": ("vf-t-193-82943-v2-r0-t1", "t-193/vf-t-193-82943/v2/r0/t1", "v2"),
    "af6aa4839592eef38": ("vf-t-193-82943-v2-r0-t2", "t-193/vf-t-193-82943/v2/r0/t2", "v2"),
}
#: the pair of verification vf-t-193-82943, try 0 (F33)
PAIR = {"v2": "a84427dcadd1ac87b", "v1": "ad6eaccc971b5eec2"}
PAIR_TRIES = {"a84427dcadd1ac87b": "t-193/vf-t-193-82943/v2/r0/t0",
              "ad6eaccc971b5eec2": "t-193/vf-t-193-82943/v1/r0/t0"}
SCOPE = "rr-004"


def home_of(root: str) -> str:
    return os.path.join(root, "rr004-home")


#: a packed fixture is the gzip bytes written as base64 text, so every shipped file is ASCII
PACKED = ".gz.b64"


def unpack(path: str) -> bytes:
    """The bytes of a fixture: a packed file is decoded and gunzipped, any other file is read."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if path.endswith(PACKED):
        raw = gzip.decompress(base64.b64decode(raw))
    return raw


def _text(path: str, root: str) -> str:
    text = unpack(path).decode("utf-8")
    return text.replace("@ROOT@", root).replace("@HOME@", home_of(root))


def records(agent: str, root: str, *, whole: bool = False, rehash: dict | None = None) -> list:
    """The records of one transcript fixture, the placeholders put back. `rehash` maps a sha256 of
    the source run to the sha256 of the same file in the test project (see `Try`)."""
    name = "%s.%sjsonl%s" % (agent, "whole." if whole else "", PACKED)
    text = _text(os.path.join(FIXTURES, name), root)
    for old, new in (rehash or {}).items():
        text = text.replace(old, new)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def calls(agent: str, root: str, *, whole: bool = False, rehash: dict | None = None) -> list:
    """The tool calls of a fixture as the judge reads them (launcher._claude_calls)."""
    return launcher._claude_calls(records(agent, root, whole=whole, rehash=rehash))


def write_transcript(agent: str, root: str, path: str, *, whole: bool = False, recs=None) -> str:
    recs = records(agent, root, whole=whole) if recs is None else recs
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for rec in recs:
            fh.write(json.dumps(rec) + "\n")
    return path


def try_dir(root: str, agent: str) -> str:
    rel = AGENTS[agent][1] if agent in AGENTS else PAIR_TRIES[agent]
    return os.path.join(root, ".alpaca", "verify", *rel.split("/"))


def prompt_path(root: str, agent: str) -> str:
    job = AGENTS[agent][0] if agent in AGENTS else {
        "a84427dcadd1ac87b": "vf-t-193-82943-v2-r0-t0",
        "ad6eaccc971b5eec2": "vf-t-193-82943-v1-r0-t0"}[agent]
    return os.path.join(root, ".alpaca", "research", SCOPE, "jobs", job, "prompt.txt")


def job_of(agent: str, root: str) -> str:
    """The job id the transcript's first record names (`ALPACA JOB <job> NONCE <nonce>`)."""
    first = records(agent, root)[0]["message"]["content"]
    return first.split()[2]


def stage(agent: str, root: str) -> str:
    """Lay the files of the agent's try folder that a test reads into <root>/.alpaca/verify: the
    packet's brief and the files its verdict reads, the receipts, the cited tree files and the
    verdict copy (the body the driver served). Returns the try folder."""
    folder = try_dir(root, agent)
    src = os.path.join(FIXTURES, agent)
    for dirpath, _dirs, files in os.walk(src):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, src)
            if rel == "ledger.json":
                continue
            target = os.path.join(folder, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if name.endswith(PACKED):
                target = target[:-len(PACKED)]
                with open(target, "wb") as out:
                    out.write(unpack(full).replace(b"@ROOT@", root.encode()).replace(
                        b"@HOME@", home_of(root).encode()))
            elif name.endswith((".json", ".patch")) or rel.startswith("packet"):
                with open(target, "w", encoding="utf-8") as out:
                    out.write(_text(full, root))
            else:
                shutil.copyfile(full, target)
    return folder


class Try:
    """One try of the control run as the judge sees it, set in a test project: the try folder, the
    calls of its stream, the driver's served ledger and the brief. The files of the try (receipts,
    the verdict copy) are laid in the project, where the project root is not the control run's, so
    their bytes and their sha256 differ from the run's; the sha256 values that name them in the
    stream and in the ledger are rewritten to the new ones (`rehash`), and nothing else changes."""

    def __init__(self, agent: str, root: str):
        self.agent, self.root = agent, root
        self.folder = stage(agent, root)
        led_text = _text(os.path.join(FIXTURES, agent, "ledger.json"), root)
        led = json.loads(led_text)
        self.rehash = {}
        rec_dir = os.path.join(self.folder, "receipts")
        for rid, old in led.get("receipts", {}).items():
            path = os.path.join(rec_dir, "%s.json" % rid)
            if os.path.isfile(path):
                self.rehash[old] = _sha(path)
        verdict = os.path.join(rec_dir, "verdict.json")
        if led.get("verdict_body") is not None:
            with open(verdict, "w", encoding="utf-8") as fh:
                fh.write(led["verdict_body"])
            self.rehash[led["verdict_sha256"]] = _sha(verdict)
        for old, new in self.rehash.items():
            led_text = led_text.replace(old, new)
        led = json.loads(led_text)
        led["requests"] = {int(k): v for k, v in led["requests"].items()}
        self.ledger = led
        self.recs = records(agent, root, rehash=self.rehash)
        self.calls = launcher._claude_calls(self.recs)
        self.try_n = int(os.path.basename(self.folder)[1:])
        with open(os.path.join(self.folder, "packet", "brief.json"), encoding="utf-8") as fh:
            self.brief = dict(json.load(fh), **{"try": self.try_n})
        self.receipts = rec_dir

    def verdict(self) -> dict:
        with open(os.path.join(self.receipts, "verdict.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def check(self, **over):
        """verdicts.check_verdict of this try's verdict (or a changed one), over its stream."""
        from alpaca.research import verdicts
        args = dict(brief=self.brief, raw=self.verdict(), receipts_dir=self.receipts,
                    stream_calls=self.calls, served=self.ledger, commands=self.brief["claims"])
        args.update(over)
        return verdicts.check_verdict(args.pop("brief"), args.pop("raw"), **args)


def _sha(path: str) -> str:
    import hashlib
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()
