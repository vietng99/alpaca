"""The standard-library receipt wrapper copied into a verifier's packet: the stand-alone request
client and the receipt and hash functions the driver serves with (design t-089, verifier.md
section 4.1); its gate re-runs go to the engine (architecture.md section 11.2).

Standard library only: it runs inside a verifier's jail with the system Python and never
imports alpaca. The driver copies this file into each packet as `packet/bin/rerun`.

    python3 packet/bin/rerun <id> [--cwd tree|base|packet] [--timeout S] -- <command> [args...]
    python3 packet/bin/rerun <id> [--cwd tree|base|packet] [--timeout S] --shell '<command text>'
    python3 packet/bin/rerun <id> --gate <stage> --side tree|base
    python3 packet/bin/rerun --gate <item> --side tree|base          (the id is derived: gate_id)
    python3 packet/bin/rerun apply --apply
    python3 packet/bin/rerun <id> --hash <packet path> --pointer <local:, receipt: or job: pointer>
    python3 packet/bin/rerun <id> --event <packet/events/N.json>     (an event: pointer, never --hash)
    python3 packet/bin/rerun --submit - <<'VERDICT'
    {"schema": "alpaca-verify-verdict/1", ...}
    VERDICT

The client writes its request as `requests/<n>.json` in the try folder (to a temporary name,
then linked into place), waits for the driver's answer `receipts/<n>.answer`, prints the
answer's one line (`rerun: receipt <id> <sha256>`, `rerun: verdict <sha256>` or
`rerun: refused <why>`) and exits with the answer's return code. Nothing else here writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time

REQUEST_SCHEMA = "alpaca-verify-request/1"
RECEIPT_SCHEMA = "alpaca-verify-receipt/1"
VERDICT_SCHEMA = "alpaca-verify-verdict/1"
EVENT_TAG = "alpaca-event/v1"
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
CWDS = ("tree", "base", "packet")
SIDES = ("tree", "base")
APPLY_ID = "apply"
#: the pointer kinds a `--hash` request can match: the brief seals their files by sha256. An
#: `event:` pointer is sealed by its row's chain hash and takes `--event` (t-296, F42).
HASH_POINTER_KINDS = ("local:", "receipt:", "job:")
#: the fixed apply step (section 5.6, item 3), run in `tree/` with the packet's diff.
APPLY_ARGV = ("git", "apply", "--binary", "--whitespace=nowarn", "--allow-empty")
POLL_S = 0.05
WAIT_MAX_S = 24 * 3600


# --------------------------------------------------------------------------- shared hashing
def canonical_json(obj) -> str:
    """The record's JSON encoding (alpaca/util.py canonical_json): sorted keys, no spaces."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def event_hashes(row: dict) -> tuple:
    """(content_hash, hash) of one exported event row, the way the chain check derives them:
    content_hash = sha256(tag + "\\n" + canonical_json(fields)) over ts, session, actor, kind,
    op, ref and data, where data is the stored text as it is, and hash = sha256(prev_hash + "\\n"
    + content_hash). The tag is the one the export names (alpaca-event/v1 unless the event was
    carried from an earlier record)."""
    fields = {k: row.get(k) for k in ("ts", "session", "actor", "kind", "op", "ref", "data")}
    tag = row.get("tag") or EVENT_TAG
    content = sha256_bytes(tag + "\n" + canonical_json(fields))
    return content, sha256_bytes(str(row.get("prev_hash") or "") + "\n" + content)


# --------------------------------------------------------------------------- requests
#: the longest id gate_id makes: the limit of ID
ID_MAX = 41
_NOT_ALNUM = re.compile(r"[^a-z0-9]+")


def _gate_mapped(item) -> str:
    return _NOT_ALNUM.sub("-", str(item).lower())


def _gate_plain(item, side: str) -> str:
    return "g-%s-%s" % (_gate_mapped(item), side)


def _gate_hashed(item, side: str) -> str:
    head = ("g-" + _gate_mapped(item))[:27].rstrip("-")
    return "%s-%s-%s" % (head, sha256_bytes(str(item).encode("utf-8"))[:8], side)


def _gate_assign(items, side: str) -> dict:
    uniq = list(dict.fromkeys(str(i) for i in items))
    plain = {i: _gate_plain(i, side) for i in uniq}
    count: dict = {}
    for v in plain.values():
        count[v] = count.get(v, 0) + 1
    ids = {i: (_gate_hashed(i, side) if (_gate_mapped(i) == "" or len(plain[i]) > ID_MAX
                                         or count[plain[i]] > 1) else plain[i]) for i in uniq}
    again: dict = {}
    for v in ids.values():
        again[v] = again.get(v, 0) + 1
    return {i: (ids[i] if again[ids[i]] == 1 else None) for i in uniq}


def gate_id(item, side: str, items=()) -> str | None:
    """The receipt id of a gate re-run asked for with no id of the agent's own
    (in-session-runtime.md section 18.3): `g-`, the item lower-cased with each run of characters
    outside [a-z0-9] turned into one hyphen, then `-` and the side, such as
    `g-metric-total-bytes-tree`. When that id is longer than ID_MAX, when the mapped item is
    empty, or when another item of `items` gives the same id, the id is `<head>-<hash>-<side>`:
    `<head>` is `g-` and the mapped item cut to its first 27 characters with a hyphen at the end of
    the cut removed, and `<hash>` the first 8 hex digits of the sha256 of the item's UTF-8 bytes.
    That is at most 41 characters on both sides, and the tree and base ids of one item differ. None
    when two items still share an id after this: neither has a derived id."""
    if side not in SIDES:
        return None
    return _gate_assign(list(items or ()) + [str(item)], side).get(str(item))


def gate_ids(items) -> dict:
    """{item: {"tree": id or None, "base": id or None}} for the gate items of a brief
    (`engine.gate_receipts`): the id each item's `--gate` request is given when it names none."""
    got = {side: _gate_assign(list(items or ()), side) for side in SIDES}
    return {str(i): {side: got[side].get(str(i)) for side in SIDES} for i in items or ()}


def parse_request(argv: list, items=()) -> dict:
    """The request `argv` asks for, or ValueError naming why it is malformed.

    Returns {"kind", "id", "cwd", "timeout", "argv", "shell", "stage", "side", "path",
    "pointer"}; kind is one of rerun, gate, apply, hash, event, submit. A request whose first
    argument is `--gate` names no id: the id is the one gate_id derives from the item and the side
    (`items` is the gate item list of the try's brief; with none, only the length rule applies)."""
    args = [str(a) for a in (argv or [])]
    out = {"kind": None, "id": None, "cwd": "tree", "timeout": None, "argv": None,
           "shell": None, "stage": None, "side": None, "path": None, "pointer": None}
    if args[:2] == ["--submit", "-"] and len(args) == 2:
        out["kind"] = "submit"
        return out
    if args == [APPLY_ID, "--apply"]:
        out.update(kind="apply", id=APPLY_ID)
        return out
    if not args:
        raise ValueError("no request")
    derived = args[0] == "--gate"
    if derived:
        i = 0
    else:
        rid = args[0]
        if not ID.match(rid):
            raise ValueError("a receipt id must match %s; to re-run a gate item with no id of "
                             "your own write: python3 packet/bin/rerun --gate <item id> --side "
                             "tree|base" % ID.pattern)
        if rid == APPLY_ID:
            raise ValueError("the id apply is kept for the apply step")
        out["id"] = rid
        i = 1
    while i < len(args):
        a = args[i]
        if a == "--":
            rest = args[i + 1:]
            if not rest:
                raise ValueError("no command after --")
            out["kind"], out["argv"] = "rerun", rest
            break
        if a in ("--cwd", "--timeout", "--shell", "--gate", "--side", "--hash", "--pointer",
                 "--event"):
            if i + 1 >= len(args):
                raise ValueError("%s needs a value" % a)
            v = args[i + 1]
            if a == "--cwd":
                if v not in CWDS:
                    raise ValueError("--cwd must be one of %s" % ", ".join(CWDS))
                out["cwd"] = v
            elif a == "--timeout":
                try:
                    out["timeout"] = float(v)
                except ValueError:
                    raise ValueError("--timeout needs a number of seconds")
                if out["timeout"] <= 0:
                    raise ValueError("--timeout must be positive")
            elif a == "--shell":
                out["kind"], out["shell"] = "rerun", v
            elif a == "--gate":
                out["kind"], out["stage"] = "gate", v
            elif a == "--side":
                if v not in SIDES:
                    raise ValueError("--side must be tree or base")
                out["side"] = v
            elif a == "--hash":
                out["kind"], out["path"] = "hash", v
            elif a == "--pointer":
                out["pointer"] = v
            elif a == "--event":
                out["kind"], out["path"] = "event", v
            i += 2
            continue
        raise ValueError("unknown argument %s" % a)
    if out["kind"] is None:
        raise ValueError("the request names no command, gate, hash, event or submission")
    if out["kind"] == "gate" and out["side"] is None:
        raise ValueError("--gate needs --side tree or --side base")
    if derived:
        if out["kind"] != "gate":
            raise ValueError("a request with no receipt id must be a --gate request")
        got = gate_id(out["stage"], out["side"], items)
        if got is None or not ID.match(got):
            raise ValueError("the gate item %s has no derived receipt id; give an id of your own "
                             "first: python3 packet/bin/rerun <id> --gate <item id> --side %s"
                             % (out["stage"], out["side"]))
        out["id"] = got
    if out["kind"] == "hash" and not out["pointer"]:
        raise ValueError("--hash needs --pointer")
    if out["kind"] == "hash":
        # an event: pointer is sealed by its row's chain hash, which no file hash of its export
        # equals, so the request can never match (F42, rr-013 event 101063); the kinds a --hash can
        # match are listed, any other is refused by default (t-296, design 25.5)
        ptr = str(out["pointer"])
        if ptr.startswith("event:"):
            raise ValueError("an event: pointer is sealed by its chain hash: write python3 "
                             "packet/bin/rerun <id> --event packet/events/<N>.json, never --hash")
        if not ptr.startswith(HASH_POINTER_KINDS):
            raise ValueError("--pointer must be a local:, receipt: or job: pointer of the brief; "
                             "an event: pointer takes --event")
    if out["kind"] in ("hash", "event"):
        p = out["path"].replace("\\", "/")
        if os.path.isabs(p) or ".." in p.split("/") or not p.startswith("packet/"):
            raise ValueError("%s must name a path inside packet/" % ("--hash" if out["kind"] == "hash" else "--event"))
        if out["kind"] == "event" and not p.startswith("packet/events/"):
            raise ValueError("--event must name a file under packet/events/")
    return out


_LIVE_WORD = re.compile(r"\$[A-Za-z_{]|`")


def request_id(args, items=(), live=()) -> str | None:
    """The receipt id an invocation of the client asks for with `args`: the first argument, `apply`
    for the apply step, the id gate_id derives for a `--gate` request, or None for a submission,
    for arguments that do not parse, and for arguments where any word holds an expansion the
    scan cannot follow. `live` has one entry for each word of `args`, parse's offsets of the
    expansions in it (None for a word brace expansion made, which counts as live when its text
    holds one); a word the loop gave no written value (`--gate $g` for `$g` from `for g in $(...)`)
    never gives a valid-looking id such as `g-g-tree`."""
    args = [str(a) for a in (args or [])]
    for k, w in enumerate(args):
        entry = live[k] if k < len(live or ()) else ()
        if entry or (entry is None and _LIVE_WORD.search(w)):
            return None
    try:
        req = parse_request(args, items)
    except ValueError:
        return None
    if req["kind"] == "submit":
        return None
    return req["id"]


def answer_line(kind: str, rid: str | None, sha: str | None, refused: str | None = None) -> str:
    if refused is not None:
        return "rerun: refused %s" % " ".join(str(refused).split())[:400]
    if kind == "submit":
        return "rerun: verdict %s" % sha
    return "rerun: receipt %s %s" % (rid, sha)


# --------------------------------------------------------------------------- the client
def _try_dir() -> str:
    here = os.path.abspath(__file__)
    return os.path.dirname(os.path.dirname(os.path.dirname(here)))


def _write_request(requests: str, body: dict) -> int:
    os.makedirs(requests, exist_ok=True)
    tmp = os.path.join(requests, ".tmp-%d-%d" % (os.getpid(), int(time.time() * 1e6)))
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(body, sort_keys=True))
    try:
        taken = [int(name[:-5]) for name in os.listdir(requests)
                 if name.endswith(".json") and name[:-5].isdigit()]
        n = max(taken) + 1 if taken else 0
        while True:
            try:
                os.link(tmp, os.path.join(requests, "%d.json" % n))
                return n
            except FileExistsError:
                n += 1
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    folder = _try_dir()
    body = {"schema": REQUEST_SCHEMA, "argv": args}
    if args[:2] == ["--submit", "-"]:
        body["body"] = sys.stdin.read()
    n = _write_request(os.path.join(folder, "requests"), body)
    answer = os.path.join(folder, "receipts", "%d.answer" % n)
    waited = 0.0
    while not os.path.isfile(answer):
        time.sleep(POLL_S)
        waited += POLL_S
        if waited > WAIT_MAX_S:
            print("rerun: refused no answer from the driver")
            return 2
    for _ in range(40):
        try:
            with open(answer, encoding="utf-8") as fh:
                got = json.loads(fh.read())
            break
        except (OSError, ValueError):
            time.sleep(POLL_S)
    else:
        print("rerun: refused unreadable answer")
        return 2
    print(got.get("line") or "rerun: refused empty answer")
    sys.stdout.flush()
    rc = got.get("rc")
    return rc if isinstance(rc, int) and 0 <= rc < 256 else 1


if __name__ == "__main__":
    sys.exit(main())
