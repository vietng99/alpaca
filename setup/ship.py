#!/usr/bin/env python3
"""Build and inspect a clean, reproducible Alpaca shipment without changing its source tree."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _helper(name):
    spec = importlib.util.spec_from_file_location("alpaca_ship_" + name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GEN = _helper("gen_manifest")
PACKAGE = _helper("package_candidate")
POLICY = _helper("shipment_policy")

# These exclusions remain binding even if a manifest mistakenly lists one as mechanism: this
# copy's runtime and development-only trees, so a stray development tree or record can never
# ride along in an Alpaca shipment.
FORBIDDEN = frozenset({
    ".git", ".alpaca", ".venv", ".pytest_cache", ".claude/worktrees",
    ".claude/settings.local.json", "RESUME.md", "analytics", "board.json", "data.json",
    "nuclear", "napalm", "docs/superpowers", "design",
})
CACHES = frozenset({"__pycache__", ".pytest_cache", ".git", ".venv"})
IDENTITY_FIELDS = frozenset({"name", "project_id", "people", "cwd_history"})
EMPTY_VALUES = frozenset({"", "null", "~", "[]", "{}", "''", '""'})


class ShipmentError(ValueError):
    pass


def _under(path, entry):
    return path == entry or path.startswith(entry.rstrip("/") + "/")


def _canonical(raw):
    raw = raw.rstrip("/")
    try:
        canonical = POLICY.canonical_staged_path(raw)
    except ValueError as exc:
        raise ShipmentError(str(exc)) from exc
    if canonical != raw or "\\" in raw:
        raise ShipmentError("non-canonical mechanism path: " + repr(raw))
    return canonical


def _classes(text):
    result = {"mechanism": [], "memory": []}
    section = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            if section not in result:
                raise ShipmentError("unknown manifest class: " + section)
        elif section:
            result[section].append(_canonical(line))
        else:
            raise ShipmentError("manifest entry outside a class: " + line)
    if not result["mechanism"] or not result["memory"]:
        raise ShipmentError("manifest needs nonempty mechanism and memory classes")
    forbidden = FORBIDDEN | set(result["memory"])
    for entry in result["mechanism"]:
        if any(_under(entry, bad) for bad in forbidden):
            raise ShipmentError("forbidden mechanism path: " + entry)
    return result


def _fresh_project(raw):
    """Accept only empty instance fields in the simple shipped YAML template, without PyYAML.

    Complex YAML values for these fields fail closed. This validator is intentionally narrower
    than the runtime project parser: building a distribution does not require dependencies.
    """
    text = raw.decode("utf-8")
    template_values = re.findall(r"(?m)^template:\s*([^\n#]*)(?:#.*)?$", text)
    template = len(template_values) == 1 and template_values[0].strip() == "true"
    current = None
    seen = set()
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^([a-z_]+):\s*(.*?)\s*$", line)
        if match:
            key, value = match.groups()
            current = key if key in IDENTITY_FIELDS else None
            if current and current in seen:
                raise ShipmentError("project identity field is repeated: " + current)
            if current:
                seen.add(current)
            value = value.split("#", 1)[0].strip()
            allowed_template = template and (
                key == "name" and value.strip("'\"") == PACKAGE.EXPECTED_PRODUCT or
                key == "project_id" and value.strip("'\"") == "alpaca-template")
            if current and value not in EMPTY_VALUES and not allowed_template:
                raise ShipmentError("project identity is populated: " + current)
        elif current:
            raise ShipmentError("project identity has nested values: " + current)
        elif not line.startswith((" ", "\t", "-")):
            raise ShipmentError("project identity requires plain YAML keys without aliases")
    if not text.strip() or text.lstrip().startswith(("{", "[", "---")):
        raise ShipmentError("project identity must use the shipped block-style template")


def _validate_content(content):
    try:
        classes = _classes(content[GEN.MANIFEST_NAME].decode("utf-8"))
        project = content["project.yaml"]
    except KeyError as exc:
        raise ShipmentError("missing required shipment member: " + str(exc)) from exc
    forbidden = FORBIDDEN | set(classes["memory"])
    for rel in content:
        if rel == GEN.LOCK_NAME:
            continue
        if any(_under(rel, bad) for bad in forbidden) or any(p in CACHES for p in PurePosixPath(rel).parts):
            raise ShipmentError("forbidden shipment member: " + rel)
        if not any(_under(rel, entry) for entry in classes["mechanism"]):
            raise ShipmentError("member absent from mechanism allowlist: " + rel)
    for entry in classes["mechanism"]:
        if not any(_under(rel, entry) for rel in content):
            raise ShipmentError("missing mechanism path: " + entry)
    _fresh_project(project)
    queue = content.get("intents/queue.md", b"").decode("utf-8")
    if re.search(r"(?m)^\s*-\s+\[[ xX]\]", queue):
        raise ShipmentError("intent queue contains project tasks; restore the fresh template")


def _selection(root):
    classes = _classes((root / GEN.MANIFEST_NAME).read_text(encoding="utf-8"))
    forbidden = FORBIDDEN | set(classes["memory"])
    selected = {}

    def add(path):
        rel = path.relative_to(root).as_posix()
        if any(_under(rel, memory) for memory in classes["memory"]):
            return
        if path.is_symlink():
            raise ShipmentError("symlink in mechanism: " + rel)
        if any(p in CACHES for p in path.relative_to(root).parts) or rel.endswith(".pyc"):
            return
        if any(_under(rel, bad) for bad in forbidden):
            raise ShipmentError("forbidden shipment member: " + rel)
        if not path.is_file():
            raise ShipmentError("non-regular mechanism member: " + rel)
        selected[rel] = path

    for entry in classes["mechanism"]:
        path = root / entry
        for parent in (path, *path.parents):
            if parent == root:
                break
            if parent.is_symlink():
                raise ShipmentError("symlink in mechanism: " + entry)
        if not path.exists():
            raise ShipmentError("missing mechanism path: " + entry)
        if path.is_dir():
            for base, dirs, names in os.walk(path, followlinks=False):
                dirs[:] = [name for name in dirs if not any(
                    _under((Path(base) / name).relative_to(root).as_posix(), memory)
                    for memory in classes["memory"])]
                for name in dirs:
                    if (Path(base) / name).is_symlink():
                        raise ShipmentError("symlink in mechanism directory: " + name)
                dirs[:] = sorted(name for name in dirs if name not in CACHES)
                for name in sorted(names):
                    add(Path(base) / name)
        else:
            add(path)
    _validate_content({rel: path.read_bytes() for rel, path in selected.items()})
    return selected


def build(root, archive):
    root, archive = Path(root).resolve(), Path(archive).absolute()
    receipt = Path(str(archive) + ".sha256")
    if archive.exists() or archive.is_symlink() or receipt.exists() or receipt.is_symlink():
        raise ShipmentError("archive or receipt already exists; choose a new output name")
    archive = archive.resolve()
    receipt = Path(str(archive) + ".sha256")
    if archive.is_relative_to(root):
        raise ShipmentError("archive output must be outside the distribution source")
    selected = _selection(root)
    with tempfile.TemporaryDirectory(prefix="alpaca-shipment-") as temporary:
        stage = Path(temporary)
        for rel, source in selected.items():
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)
        _validate_content({rel: (stage / rel).read_bytes() for rel in selected})
        if GEN.write(str(stage)):
            raise ShipmentError("cannot generate staged integrity manifest")
        digest, count = PACKAGE.build_archive(stage, archive, receipt)
    verify(archive)
    return digest, count


def verify(archive):
    archive = Path(archive)
    try:
        manifest, _, content = PACKAGE.verified_archive(archive)
        if not PACKAGE.cross_check_receipt(archive):
            raise ShipmentError("missing SHA256 receipt beside archive")
    except (PACKAGE.PackageError, OSError, ValueError) as exc:
        raise ShipmentError(str(exc)) from exc
    _validate_content(content)
    return manifest, content


def extract(archive, destination):
    destination = Path(destination).absolute()
    if destination.is_symlink() or destination.exists():
        raise ShipmentError("extraction destination exists; choose a new directory")
    manifest, content = verify(archive)
    destination.mkdir(parents=True)
    for rel, raw in sorted(content.items()):
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        target.chmod(0o644 if rel == GEN.LOCK_NAME else PACKAGE.expected_mode(manifest, rel))
    # Use the current trusted verifier; inspection never executes code from an archive.
    PACKAGE.verify_selected_tree(destination, exact=True)
    measured, walk_errors, read_errors = GEN.build(str(destination))
    if walk_errors or read_errors or measured != manifest:
        raise ShipmentError("extracted distribution does not match its mechanism manifest")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    builder = sub.add_parser("build", help="build outside this source from the explicit allowlist")
    builder.add_argument("--root", type=Path, default=ROOT)
    builder.add_argument("--archive", type=Path, required=True)
    checker = sub.add_parser("verify", help="verify archive, receipt, allowlist, and fresh identity")
    checker.add_argument("archive", type=Path)
    unpack = sub.add_parser("extract", help="verify and extract into a new directory")
    unpack.add_argument("archive", type=Path)
    unpack.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            digest, count = build(args.root, args.archive)
            print("Alpaca shipment: %s (%d files)" % (args.archive, count))
            print("SHA256: " + digest)
        elif args.command == "verify":
            manifest, content = verify(args.archive)
            print("Alpaca shipment verified: %d files; %s" % (len(content), manifest["tree_digest"]))
        else:
            extract(args.archive, args.destination)
            print("Alpaca shipment extracted: " + str(args.destination))
        return 0
    except (ShipmentError, PACKAGE.PackageError, OSError, ValueError) as exc:
        print("Alpaca shipment refused: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
