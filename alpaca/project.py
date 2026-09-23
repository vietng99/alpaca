import json
import os
import uuid
from alpaca import db, util

FILE = "project.yaml"
DEFAULT_PHASES = ["requirement", "design", "build", "verify", "release"]

def path(root):
    return os.path.join(root, FILE)

def load(root) -> dict:
    p = path(root)
    if not os.path.isfile(p):
        return {}
    import yaml
    with open(p, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if cfg.get("template") is True:
        local = os.path.join(root, ".alpaca", "instance.json")
        if os.path.isfile(local):
            with open(local, encoding="utf-8") as fh:
                cfg["project_id"] = json.load(fh)["project_id"]
    return cfg


def ensure_instance(root) -> dict:
    """Give each clean shipment its own identity without changing shipped configuration."""
    import fcntl
    directory = os.path.join(root, ".alpaca")
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, "instance.json")
    with open(os.path.join(directory, "instance.lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if os.path.isfile(target):
            with open(target, encoding="utf-8") as fh:
                return json.load(fh)
        identity = {"project_id": uuid.uuid4().hex, "created": util.now_iso()}
        util.write_text(target, json.dumps(identity, indent=2) + "\n")
        return identity

def save(root, cfg: dict) -> None:
    import yaml
    util.write_text(path(root), yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))

def is_onboarded(root) -> bool:
    return db.meta_get(db.connect(root), "onboarded") is not None

def detect_commands(root) -> dict:
    """Best-effort build/test/lint/run commands from files present. Never guesses beyond these."""
    out = {}
    has = lambda *n: any(os.path.isfile(os.path.join(root, x)) for x in n)
    if has("Makefile"):
        txt = util.read_text(os.path.join(root, "Makefile"))
        for t in ("build", "test", "lint", "run"):
            if ("\n%s:" % t) in ("\n" + txt):
                out[t] = "make %s" % t
    if has("pyproject.toml", "pytest.ini", "setup.cfg") and "test" not in out:
        out["test"] = "python3 -m pytest"
    if has("package.json"):
        out.setdefault("test", "npm test"); out.setdefault("build", "npm run build")
    return out

def validate(root) -> tuple:
    """Load project.yaml and validate it against the M1.8 schema. Returns (ok, errors).

    A thin convenience over alpaca.project_schema.validate so callers that already have a root
    (alpaca doctor, the phase doors) do not repeat the load. The schema is defined once in
    alpaca/project_schema.py; this never restates it.
    """
    from alpaca import project_schema
    return project_schema.validate(load(root))
