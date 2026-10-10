"""Fakes for the launcher tests (t-098): a fake Claude and a fake Codex runtime, written as small
Python scripts, and a lab that lays out a run's folders the way the engine does.

The fake runtimes never reach a model or the network. Each reads its prompt from stdin, finds one
line `FAKE <json>` in it and acts on that directive: report what it can see, sleep, leave a child
that escapes its process group, plant a link, or exit with given lines. Without a directive it
behaves like a runtime that finished one turn. A test module, not a conftest: import it.
"""
from __future__ import annotations

import json
import os
import stat
import types as pytypes

from alpaca import db
from alpaca.research import paths
from alpaca.research import types as rtypes

RUN = "rr-001"
ATTEMPT = "t-140"
OTHER = "t-141"

_COMMON = r'''
import hashlib, json, os, socket, sys, time

ARGS = sys.argv[1:]
PROMPT = "" if (ARGS[:2] == ["features", "list"]) else sys.stdin.read()
D = {}
for _line in PROMPT.splitlines():
    if _line.startswith("FAKE "):
        D = json.loads(_line[5:])
        break
MODE = D.get("mode", "ok")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def arg(name):
    return ARGS[ARGS.index(name) + 1] if name in ARGS else None


def probe():
    rep = {"type": "probe", "cwd": os.getcwd(), "env": dict(os.environ), "argv": sys.argv,
           "stdin_sha256": hashlib.sha256(PROMPT.encode("utf-8")).hexdigest(),
           "root_listing": sorted(os.listdir("/")), "exists": {}, "reads": {}, "writes": {},
           "connect": {}, "proxy": {}}
    for p in D.get("exists", []):
        rep["exists"][p] = os.path.exists(p)
    rep["lists"] = {}
    for p in D.get("list", []):
        try:
            rep["lists"][p] = sorted(os.listdir(p))
        except Exception as exc:
            rep["lists"][p] = "ERR " + type(exc).__name__
    for p in D.get("read", []):
        try:
            with open(p, "rb") as fh:
                rep["reads"][p] = fh.read(200).decode("utf-8", "replace")
        except Exception as exc:
            rep["reads"][p] = "ERR " + type(exc).__name__
    rep["slurps"] = {}
    for p in D.get("slurp", []):
        try:
            with open(p, "rb") as fh:
                rep["slurps"][p] = fh.read(1 << 20).decode("utf-8", "replace")
        except Exception as exc:
            rep["slurps"][p] = "ERR " + type(exc).__name__
    for p in D.get("write", []):
        try:
            with open(p, "a") as fh:
                fh.write("x")
            rep["writes"][p] = "ok"
        except Exception as exc:
            rep["writes"][p] = "ERR " + type(exc).__name__
    for host, port in D.get("connect", []):
        try:
            s = socket.create_connection((host, port), timeout=3)
            s.close()
            rep["connect"]["%s:%d" % (host, port)] = "connected"
        except Exception as exc:
            rep["connect"]["%s:%d" % (host, port)] = "ERR " + type(exc).__name__
    for target in D.get("proxy", []):
        try:
            s = socket.create_connection(("127.0.0.1", 8119), timeout=5)
            s.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (target, target)).encode())
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = s.recv(1024)
                if not chunk:
                    break
                head += chunk
            status = head.split(b"\r\n", 1)[0].decode()
            echoed = None
            if " 200 " in status + " ":
                s.sendall(b"ping\n")
                echoed = s.recv(100).decode()
            s.close()
            rep["proxy"][target] = {"status": status, "echo": echoed}
        except Exception as exc:
            rep["proxy"][target] = {"status": "ERR " + type(exc).__name__, "echo": None}
    emit(rep)


def escape(clean=False):
    """Leave a double-forked child in a session of its own; clean: its environment is empty,
    so no launch tag marks it."""
    token = D["token"]
    pid = os.fork()
    if pid == 0:
        os.setsid()
        if os.fork() == 0:
            if clean:
                os.execve("/usr/bin/sleep", ["/usr/bin/sleep", token], {})
            os.execv("/usr/bin/sleep", ["/usr/bin/sleep", token])
        os._exit(0)
    os.waitpid(pid, 0)
    emit({"type": "escaped", "token": token})


# a tool child that tries to reopen the runtime's stdout and stdin through /proc/<ppid>/fd
TAMPER_CHILD = """
import json, os
ppid = os.getppid()
rep = {}


def attempt(name, fn):
    try:
        rep[name] = fn()
    except OSError as exc:
        rep[name] = "ERR %s errno=%s" % (type(exc).__name__, exc.errno)


def overwrite_stream():
    fd = os.open("/proc/%d/fd/1" % ppid, os.O_WRONLY)
    with open("/proc/%d/fd/1" % ppid, "rb") as fh:
        data = fh.read()
    mark = data.index(b"EVIDENCE-OF-TAMPER")
    start = data.rindex(b"\\n", 0, mark) + 1
    end = data.index(b"\\n", start)
    fake = b'{"type": "note"}'
    os.pwrite(fd, fake + b" " * (end - start - len(fake)), start)
    return "ok"


def read_stream():
    os.close(os.open("/proc/%d/fd/1" % ppid, os.O_RDONLY | os.O_NONBLOCK))
    return "ok"


def truncate_prompt():
    fd = os.open("/proc/%d/fd/0" % ppid, os.O_WRONLY | os.O_TRUNC)
    os.write(fd, b"FORGED PROMPT\\n")
    return "ok"


attempt("overwrite_stream", overwrite_stream)
attempt("read_stream", read_stream)
attempt("truncate_prompt", truncate_prompt)
print(json.dumps(rep))
"""


def fd_links():
    """Every /proc/<pid>/fd link this process can read: {"<pid>/<fd>": target}."""
    links = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            for fd in os.listdir("/proc/%s/fd" % name):
                links["%s/%s" % (name, fd)] = os.readlink("/proc/%s/fd/%s" % (name, fd))
        except OSError:
            continue
    return links


def tamper():
    """Check round 3 of t-098 (B1): write a Bash tool_use line, then let a tool child and a
    shell try to rewrite it, the stream's head and the prompt through /proc/<pid>/fd links."""
    import subprocess
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tu_t",
          "name": "Bash", "input": {"command": "cat /EVIDENCE-OF-TAMPER"}}]}})
    kid = subprocess.run(["/usr/bin/python3", "-c", TAMPER_CHILD], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    shell = subprocess.run(
        ["/bin/sh", "-c", "printf '{\"type\":\"forged-by-shell\"}\\n' "
         "1<>/proc/%s/fd/1 && echo ok || echo ERR" % D.get("shell_pid", 2)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    emit({"type": "tamper", "pid": os.getpid(), "child": kid.stdout.decode(),
          "shell": shell.stdout.decode(), "fds": fd_links()})


def flood():
    """Check round 1 of t-098 (adversarial B2): one stdout line of D["mb"] MiB with no line
    break, the credential value written into it at the given offsets (one of them across a
    1 MiB block edge), then the line break."""
    value = os.environ.get(D.get("var", "ANTHROPIC_API_KEY"), "").encode()
    size = int(D["mb"]) * 1024 * 1024
    marks = sorted(int(o) for o in D.get("at", []))
    block = b"x" * (1024 * 1024)
    done = 0
    while done < size:
        n = min(len(block), size - done)
        chunk = bytearray(block[:n])
        for off in marks:
            if done <= off < done + n:
                part = value[:done + n - off]
                chunk[off - done:off - done + len(part)] = part
            elif off < done < off + len(value):
                part = value[done - off:done - off + n]
                chunk[:len(part)] = part
        view = memoryview(bytes(chunk))
        while view:
            view = view[os.write(1, view):]
        done += n
    os.write(1, b"\n")


def common():
    if MODE == "probe":
        probe()
    elif MODE == "flood":
        flood()
    elif MODE == "tamper":
        tamper()
    elif MODE in ("escape", "escape_clean"):
        escape(clean=MODE == "escape_clean")
        if D.get("then") == "sleep":
            time.sleep(120)
    elif MODE == "leak":
        # copy the runtime's credential into the worktree and print it
        value = os.environ.get(D.get("var", "ANTHROPIC_API_KEY"), "")
        with open(D["into"], "w") as fh:
            fh.write("stolen=%s\n" % value)
        if D.get("backdate"):
            os.utime(D["into"], (time.time() - 3600, time.time() - 3600))
        emit({"type": "leak", "value": value})
    elif MODE == "sparse_tree":
        # check round 4 of t-098 (adversarial B1): the credential at the start of many files
        # made large with truncate, which costs the agent no disk, and one small copy of it
        value = os.environ.get(D.get("var", "ANTHROPIC_API_KEY"), "").encode()
        os.makedirs(D["into"], exist_ok=True)
        for i in range(int(D["files"])):
            with open(os.path.join(D["into"], "s%04d.bin" % i), "wb") as fh:
                fh.write(value)
                fh.truncate(int(D["bytes"]))
        with open(os.path.join(D["into"], "small.txt"), "wb") as fh:
            fh.write(b"stolen=" + value + b"\n")
        if D.get("dense"):
            with open(os.path.join(D["into"], "dense.bin"), "wb") as fh:
                fh.write(value + b"." * (int(D["dense"]) - len(value)))
        emit({"type": "sparse_tree", "files": int(D["files"])})
    elif MODE == "sleep":
        time.sleep(float(D.get("seconds", 120)))
    elif MODE == "lines":
        for line in D.get("lines", []):
            sys.stdout.write(line + "\n")
        sys.stdout.flush()
        sys.exit(int(D.get("rc", 0)))
'''

_CLAUDE = _COMMON + r'''

def main():
    config = os.environ["CLAUDE_CONFIG_DIR"]
    sid = arg("--session-id")
    if D.get("bare"):
        common()                   # a runtime that exits before its first stream event
    emit({"type": "system", "subtype": "init", "session_id": sid, "model": "fake-model",
          "cwd": os.getcwd()})
    common()
    folder = os.path.join(config, "projects", "-fake-project")
    os.makedirs(folder, exist_ok=True)
    transcript = os.path.join(folder, sid + ".jsonl")
    if MODE == "link_transcript":
        os.symlink(D["target"], transcript)
    elif MODE == "no_transcript":
        pass                       # the agent removed its transcript, or the runtime wrote none
    elif D.get("measured"):
        # a transcript metrics.analyze measures (it needs uuid, timestamp and requestId)
        with open(transcript, "w") as fh:
            fh.write(json.dumps({"type": "user", "sessionId": sid, "uuid": "u-1",
                                 "timestamp": "2026-09-30T00:00:00Z",
                                 "message": {"role": "user", "content": "hi"}}) + "\n")
            fh.write(json.dumps({"type": "assistant", "sessionId": sid, "uuid": "u-2",
                                 "parentUuid": "u-1", "requestId": "req_1",
                                 "timestamp": "2026-09-30T00:00:01Z", "message": {
                "id": "msg_1", "role": "assistant", "model": "fake-model",
                "content": [{"type": "text", "text": "done"}],
                "usage": {"input_tokens": 10, "output_tokens": 5,
                          "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 0}}}) + "\n")
    else:
        with open(transcript, "w") as fh:
            fh.write(json.dumps({"type": "user", "sessionId": sid,
                                 "message": {"role": "user", "content": "hi"}}) + "\n")
            fh.write(json.dumps({"type": "assistant", "sessionId": sid, "message": {
                "id": "msg_1", "role": "assistant", "model": "fake-model",
                "content": [{"type": "text", "text": "done"}],
                "usage": {"input_tokens": 10, "output_tokens": 5}}}) + "\n")
    if MODE == "sparse":
        # check round 2 of t-098 (adversarial B2): a transcript or subagent file made huge with
        # truncate, which costs the agent no disk
        target = transcript
        if D.get("where") == "subagent":
            deep = os.path.join(folder, sid, "subagents")
            os.makedirs(deep, exist_ok=True)
            target = os.path.join(deep, "agent-1.jsonl")
            with open(target, "w") as fh:
                fh.write(json.dumps({"type": "user", "sessionId": sid}) + "\n")
        os.truncate(target, int(D["bytes"]))
        with open(target, "a") as fh:
            fh.write("\n")
    if MODE in ("subagent_link", "subagent_ok", "subagent_deep_link"):
        side = os.path.join(folder, sid)
        os.makedirs(side, exist_ok=True)
        if MODE == "subagent_link":
            os.symlink(D["target"], os.path.join(side, "subagents"))
        else:
            deep = os.path.join(side, "subagents", "task-1")
            os.makedirs(deep, exist_ok=True)
            with open(os.path.join(deep, "agent-1.jsonl"), "w") as fh:
                fh.write(json.dumps({"type": "user", "sessionId": sid,
                                     "message": {"role": "user", "content": "sub"}}) + "\n")
            if MODE == "subagent_deep_link":
                os.symlink(D["target"], os.path.join(deep, "nested"))
    emit({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "I will look."},
        {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "ls -la"}}]}})
    emit({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "total 0"}]}})
    emit({"type": "result", "subtype": "success", "is_error": False,
          "result": D.get("result", "done"),
          "total_cost_usd": D.get("cost", 0.01),
          "usage": D.get("usage", {"input_tokens": 10, "output_tokens": 5})})


main()
'''

_CODEX = _COMMON + r'''
FEATURES = __FEATURES__


def main():
    if ARGS[:2] == ["features", "list"]:
        for name, value in FEATURES.items():
            print("%-40s %-18s %s" % (name, "stable", "true" if value else "false"))
        return
    home = os.environ["CODEX_HOME"]
    thread = "0199a0e0-0000-7000-8000-000000000001"
    emit({"type": "thread.started", "thread_id": thread})
    emit({"type": "turn.started"})
    common()
    sessions = os.path.join(home, "sessions")
    if MODE == "link_sessions":
        os.symlink(D["target"], sessions)
    elif MODE == "no_transcript":
        pass
    else:
        folder = os.path.join(sessions, "2026", "09", "30")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "rollout-2026-09-30T00-00-00-%s.jsonl" % thread), "w") as fh:
            fh.write(json.dumps({"type": "session_meta",
                                 "payload": {"id": thread, "cwd": os.getcwd()}}) + "\n")
        if MODE == "special_rollout":
            # check round 3 of t-098 (adversarial B1): beside the real rollout, a file named for
            # the thread that is a FIFO (a plain open() of it waits for a writer that never
            # comes) or a link; kind None plants nothing (the control)
            beside = os.path.join(folder, "x-%s.jsonl" % thread)
            if D.get("kind") == "fifo":
                os.mkfifo(beside)
            elif D.get("kind") == "link":
                os.symlink(D["target"], beside)
    last = arg("--output-last-message")
    if MODE == "link_message":
        os.symlink(D["target"], last)
    else:
        with open(last, "w") as fh:
            fh.write("final note from codex")
    emit({"type": "item.started", "item": {"id": "item_1", "type": "command_execution",
          "command": "bash -lc 'cat README'", "aggregated_output": "", "status": "in_progress"}})
    emit({"type": "item.completed", "item": {"id": "item_1", "type": "command_execution",
          "command": "bash -lc 'cat README'", "aggregated_output": "hello\n", "exit_code": 0,
          "status": "completed"}})
    emit({"type": "item.completed", "item": {"id": "item_2", "type": "agent_message",
          "text": "final note from codex"}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 20, "cached_input_tokens": 4,
          "output_tokens": 7}})


main()
'''

CODEX_FEATURES = ("memories", "apps", "browser_use", "browser_use_external",
                  "browser_use_full_cdp_access", "computer_use", "hooks", "image_generation",
                  "plugins", "remote_plugin", "multi_agent", "skill_search",
                  "skill_mcp_dependency_install", "tool_suggest", "shell_snapshot", "worktrees")


def _script(path: str, body: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/usr/bin/python3\n" + body)
    os.chmod(path, 0o755)
    return path


def fake_claude(base: str) -> str:
    """A fake Claude binary at <base>/claude/versions/9.9.9 (a single file, as Claude keeps it)."""
    return _script(os.path.join(base, "claude", "versions", "9.9.9"), _CLAUDE)


def fake_codex(base: str, *, on=()) -> str:
    """A fake Codex binary in a versioned release folder; features in `on` list as true."""
    features = {name: name in on for name in CODEX_FEATURES}
    folder = os.path.join(base, "codex", "releases", "1.2.3-x86_64-fake", "bin")
    return _script(os.path.join(folder, "codex"), _CODEX.replace("__FEATURES__", repr(features)))


def directive(**kw) -> str:
    """A prompt carrying one FAKE directive line, among ordinary text."""
    return "Some rendered prompt text.\nFAKE %s\nMore text.\n" % json.dumps(kw)


class Lab:
    """A project root with one run laid out as the engine lays it out (section 8.4)."""

    def __init__(self, root: str, tmp: str):
        self.root = root
        self.tmp = tmp
        self.attempt_dir = paths.attempt_dir(root, RUN, ATTEMPT)
        self.worktree = paths.worktree_dir(root, RUN, ATTEMPT)
        self.other_tree = paths.worktree_dir(root, RUN, OTHER)
        self.other_attempt = paths.attempt_dir(root, RUN, OTHER)
        self.clone = paths.clone_dir(root, RUN)
        self.git_common = os.path.join(self.clone, ".git")
        self.git_admin = os.path.join(self.attempt_dir, "git", "admin")
        self.git_objects = os.path.join(self.attempt_dir, "git", "objects")
        self.notes = os.path.join(self.attempt_dir, "notes")
        self.champion = paths.champion_dir(root, RUN, 1)
        self.champion_admin = self.champion + ".git-admin"
        self.contract_dir = paths.contract_dir(root, RUN)
        self.audit = os.path.join(paths.run_dir(root, RUN), "audit", ATTEMPT)
        self.repair = os.path.join(paths.run_dir(root, RUN), "repair", "t-150")
        self.verify = os.path.join(root, ".alpaca", "verify", ATTEMPT, "vf-t-140-1")
        self.refdata = os.path.join(tmp, "refdata")
        for folder in (self.worktree, self.other_tree, self.other_attempt, self.git_admin,
                       self.git_objects, self.notes, os.path.join(self.git_common, "worktrees"),
                       os.path.join(self.git_common, "objects"), self.champion,
                       self.champion_admin, self.contract_dir, self.audit, self.repair,
                       self.verify, self.refdata):
            os.makedirs(folder, exist_ok=True)
        self.write(os.path.join(self.worktree, "README"), "attempt tree\n")
        self.write(os.path.join(self.worktree, ".git"), "gitdir: %s\n" % self.git_admin)
        self.write(os.path.join(self.git_admin, "commondir"), self.git_common + "\n")
        self.write(os.path.join(self.other_tree, "SECRET"), "another attempt\n")
        self.write(os.path.join(self.contract_dir, "contract.json"), "{}\n")
        self.write(os.path.join(self.refdata, "table.txt"), "reference data\n")
        self.write(os.path.join(root, "CLAUDE.md"), "project instructions\n")
        self.contract = pytypes.SimpleNamespace(
            read_paths=(self.refdata,), read_real=(os.path.realpath(self.refdata),),
            egress_extra=(), note_chars=300)
        self.conn = db.connect(root)

    @staticmethod
    def write(path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def implementer(self, **kw) -> rtypes.LaunchSpec:
        base = dict(role="implementer", runtime="claude", session="%s-%s-implementer-t1"
                    % (RUN, ATTEMPT), workdir=self.worktree, cwd=self.worktree,
                    prompt=directive(), timeout_s=60, key="%s/implementer/t1" % ATTEMPT,
                    run=RUN, attempt=ATTEMPT, read_paths=(self.refdata,),
                    write_paths=(self.worktree,), tools=("Read", "Bash", "Write", "Edit"),
                    git_objects=self.git_objects,
                    git_alternates=os.path.join(self.git_common, "objects"),
                    git_admin=self.git_admin, git_common=self.git_common)
        base.update(kw)
        return rtypes.LaunchSpec(**base)

    def events(self, kind: str) -> list:
        return db.events(self.conn, kind=kind, limit=10 ** 6)


def go_check(*, posture="GO", usd_cap=5.0, usd_spent=1.0):
    return rtypes.SpendCheck(posture=posture, reason="budget test" if posture == "HOLD" else "",
                             usd_spent=usd_spent, tokens_spent=0, usd_cap=usd_cap,
                             tokens_cap=None, wall_s_spent=0.0, wall_cap_s=None, unpriced=0,
                             project_posture="GO")


def is_link(path: str) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False
