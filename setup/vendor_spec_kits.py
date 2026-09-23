#!/usr/bin/env python3
"""vendor_spec_kits.py - rebuild vendor/ from the pinned spec-kit and OpenSpec releases.

A maintainer tool. It needs the network (github.com, the npm registry, and PyPI for the
dependencies of spec-kit's own CLI), python3 with venv, git and node. Nothing in it runs at use
time: `alpaca spec init` installs either kit offline from the files this script writes.

spec-kit (https://github.com/github/spec-kit, MIT):
  downloads the tag archive, installs the `specify` CLI from that archive into a scratch venv,
  runs `specify init` for the Claude integration in a scratch directory, and packs the output into
  one archive with fixed member order, owners and times. The upstream LICENSE is copied next to it.
OpenSpec (https://github.com/Fission-AI/OpenSpec, MIT):
  downloads a pinned npm CLI tarball (used here only to resolve the dependency tree), resolves the
  production dependencies of the pinned npm package, downloads every package tarball from the
  registry and checks it against the lockfile integrity. The upstream LICENSE is copied next to them.

vendor/VENDOR.json records, per tool, the repository URL, the tag, the commit, the sha256 of the tag
archive, and for every vendored archive the sha256 of the stored file and of the tar inside it.
`vendor/spec-kit/constitution.md` is Alpaca's own file and is left alone.

With --scan-terms (a term list in the leak-audit format: one case-insensitive substring per line,
`#re:` lines as case-insensitive regexes), a stored archive whose compressed bytes happen to match
a term is compressed again at another gzip level until it does not. The tar inside is unchanged,
so `tar_sha256` still equals the tar of the registry tarball; the entry records `stored`.

Usage:
    python3 setup/vendor_spec_kits.py --scratch <dir> [--out <repo>/vendor] [--scan-terms <file>]
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import platform
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request

SPEC_KIT = {
    "upstream": "https://github.com/github/spec-kit",
    "tag": "v1.0.10",
    "license": "MIT",
}
OPENSPEC = {
    "upstream": "https://github.com/Fission-AI/OpenSpec",
    "tag": "v1.13.1",
    "npm_package": "@fission-ai/openspec",
    "version": "1.13.1",
    "license": "MIT",
}
#: the npm CLI used only to resolve OpenSpec's dependency tree at vendoring time (not vendored).
NPM_TOOL_VERSION = "11.20.0"
REGISTRY = "https://registry.npmjs.org"
SPECIFY_INIT = ["init", "--here", "--force", "--non-interactive", "--integration", "claude",
                "--script", "sh", "--ignore-agent-tools"]
NPM_INSTALL = ["install", "--omit=dev", "--omit=optional", "--ignore-scripts", "--no-audit",
               "--no-fund", "--save-exact"]
LICENSE_NAMES = ("license", "license.md", "license.txt", "licence", "licence.md", "copying")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def load_scan_terms(path):
    """(substrings, regexes) from a leak-audit term list; ([], []) without a path."""
    subs, rxs = [], []
    if not path:
        return subs, rxs
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#re:"):
                rxs.append(re.compile(line[4:].strip(), re.I))
                continue
            t = line.split("  #", 1)[0].strip()
            if t and not t.startswith(("#", "!")):
                subs.append(t.casefold())
    return subs, rxs


def term_hits(data, name, terms):
    """How many terms match the bytes (read as UTF-8 with replacement and as Latin-1) or the name."""
    subs, rxs = terms
    hits = 0
    for text in (data.decode("utf-8", "replace"), data.decode("latin-1")):
        text = name + "\n" + text
        low = text.casefold()
        hits += sum(1 for t in subs if t in low) + sum(1 for rx in rxs if rx.search(text))
    return hits


def store_gzip(tar_bytes, dst, terms, original=None):
    """Write the gzip of tar_bytes to dst: the original bytes when they match no term, else the first
    gzip level (9 down to 1, no name, time 0) whose output matches no term. Returns `stored`."""
    name = os.path.basename(dst)
    if original is not None and not term_hits(original, name, terms):
        with open(dst, "wb") as fh:
            fh.write(original)
        return "registry"
    for level in range(9, 0, -1):
        buf = io.BytesIO()
        with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=level) as gz:
            gz.write(tar_bytes)
        data = buf.getvalue()
        if not term_hits(data, name, terms):
            with open(dst, "wb") as fh:
                fh.write(data)
            return "gzip level %d" % level
    raise SystemExit("no gzip level gives %s without a term match" % name)


def fetch(url, dst):
    if not os.path.isfile(dst):
        with urllib.request.urlopen(url, timeout=120) as resp, open(dst + ".part", "wb") as out:
            shutil.copyfileobj(resp, out)
        os.replace(dst + ".part", dst)
    return dst


def run(cmd, cwd=None, env=None, log=None):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("$ %s\n%s%s\n[rc=%d]\n" % (" ".join(cmd), p.stdout, p.stderr, p.returncode))
    if p.returncode != 0:
        raise SystemExit("command failed (%d): %s\n%s" % (p.returncode, " ".join(cmd), p.stderr))
    return p.stdout


def tag_commit(upstream, tag):
    """The commit a tag points at (the peeled ref of an annotated tag), from git ls-remote."""
    out = run(["git", "ls-remote", upstream + ".git", "refs/tags/%s" % tag, "refs/tags/%s^{}" % tag])
    refs = dict((line.split("\t")[1], line.split("\t")[0]) for line in out.splitlines() if "\t" in line)
    return refs.get("refs/tags/%s^{}" % tag) or refs["refs/tags/%s" % tag]


def pack_tree(src, dst, terms=([], [])):
    """Pack every regular file under src into dst: sorted members, uid/gid 0, mtime 0, mode 644
    or 755, and a gzip header without a name or a time, so the same tree gives the same bytes.
    Returns ({path: sha256}, sha256 of the tar, how it was stored)."""
    files = []
    for base, dirs, names in os.walk(src):
        dirs.sort()
        for n in sorted(names):
            full = os.path.join(base, n)
            if os.path.islink(full) or not os.path.isfile(full):
                raise SystemExit("refusing a non-regular file in the generated tree: %s" % full)
            files.append(os.path.relpath(full, src).replace(os.sep, "/"))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in sorted(files):
            full = os.path.join(src, rel)
            with open(full, "rb") as fh:
                data = fh.read()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if os.stat(full).st_mode & 0o111 else 0o644
            tar.addfile(info, io.BytesIO(data))
    stored = store_gzip(buf.getvalue(), dst, terms)
    digests = {}
    for rel in files:
        with open(os.path.join(src, rel), "rb") as fh:
            digests[rel] = hashlib.sha256(fh.read()).hexdigest()
    return digests, hashlib.sha256(buf.getvalue()).hexdigest(), stored


def vendor_spec_kit(scratch, out, log, terms=([], [])):
    tag = SPEC_KIT["tag"]
    version = tag.lstrip("v")
    url = "%s/archive/refs/tags/%s.tar.gz" % (SPEC_KIT["upstream"], tag)
    dl = os.path.join(scratch, "dl")
    os.makedirs(dl, exist_ok=True)
    src_archive = fetch(url, os.path.join(dl, "spec-kit-%s.tar.gz" % tag))
    venv = os.path.join(scratch, "speckit-venv")
    if not os.path.isfile(os.path.join(venv, "bin", "specify")):
        run([sys.executable, "-m", "venv", venv], log=log)
        run([os.path.join(venv, "bin", "pip"), "install", "-q", src_archive], log=log)
    cli_version = run([os.path.join(venv, "bin", "python"), "-c",
                       "import importlib.metadata as m; print(m.version('specify-cli'))"]).strip()
    if cli_version != version:
        raise SystemExit("specify-cli %s installed, expected %s" % (cli_version, version))
    gen = os.path.join(scratch, "speckit-generated")
    shutil.rmtree(gen, ignore_errors=True)
    os.makedirs(gen)
    env = dict(os.environ, COLUMNS="120", NO_COLOR="1")
    run([os.path.join(venv, "bin", "specify")] + SPECIFY_INIT, cwd=gen, env=env, log=log)
    kit_dir = os.path.join(out, "spec-kit")
    os.makedirs(kit_dir, exist_ok=True)
    archive_rel = "spec-kit/speckit-%s-claude-sh.tar.gz" % version
    files, tar_sha256, stored = pack_tree(gen, os.path.join(out, archive_rel), terms)
    with tarfile.open(src_archive) as tar:
        lic = tar.extractfile("spec-kit-%s/LICENSE" % version).read()
    with open(os.path.join(kit_dir, "LICENSE"), "wb") as fh:
        fh.write(lic)
    return {
        "upstream": SPEC_KIT["upstream"],
        "license": SPEC_KIT["license"],
        "license_file": "spec-kit/LICENSE",
        "tag": tag,
        "commit": tag_commit(SPEC_KIT["upstream"], tag),
        "source_archive": {"url": url, "sha256": sha256_file(src_archive)},
        "version": version,
        "generated_with": "specify " + " ".join(SPECIFY_INIT),
        "generator_python": platform.python_version(),
        "archive": {"path": archive_rel, "sha256": sha256_file(os.path.join(out, archive_rel)),
                    "tar_sha256": tar_sha256, "stored": stored},
        "files": files,
        "constitution": {"path": ".specify/memory/constitution.md",
                         "replaced_by": "spec-kit/constitution.md"},
    }


def _npm_file_name(name, version):
    return "%s-%s.tgz" % (name.replace("@", "").replace("/", "-"), version)


def _license_member(tgz_path):
    with tarfile.open(tgz_path) as tar:
        names = sorted(m.name for m in tar.getmembers() if m.isfile() and m.name.count("/") == 1)
    for n in names:
        if n.split("/", 1)[1].lower() in LICENSE_NAMES:
            return n
    return None


def vendor_openspec(scratch, out, log, terms=([], [])):
    dl = os.path.join(scratch, "dl")
    os.makedirs(dl, exist_ok=True)
    url = "%s/archive/refs/tags/%s.tar.gz" % (OPENSPEC["upstream"], OPENSPEC["tag"])
    src_archive = fetch(url, os.path.join(dl, "OpenSpec-%s.tar.gz" % OPENSPEC["tag"]))
    npm_tgz = fetch("%s/npm/-/npm-%s.tgz" % (REGISTRY, NPM_TOOL_VERSION),
                    os.path.join(dl, "npm-%s.tgz" % NPM_TOOL_VERSION))
    npm_dir = os.path.join(scratch, "npm-tool")
    if not os.path.isdir(npm_dir):
        os.makedirs(npm_dir)
        with tarfile.open(npm_tgz) as tar:
            tar.extractall(npm_dir, filter="data")
    npm_cli = os.path.join(npm_dir, "package", "bin", "npm-cli.js")
    resolve = os.path.join(scratch, "openspec-resolve")
    shutil.rmtree(resolve, ignore_errors=True)
    os.makedirs(resolve)
    with open(os.path.join(resolve, "package.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": "openspec-vendor-resolve", "private": True, "version": "0.0.0"}, fh)
    spec = "%s@%s" % (OPENSPEC["npm_package"], OPENSPEC["version"])
    env = dict(os.environ, npm_config_cache=os.path.join(scratch, "npm-cache"),
               npm_config_update_notifier="false")
    run(["node", npm_cli] + NPM_INSTALL + [spec], cwd=resolve, env=env, log=log)
    with open(os.path.join(resolve, "package-lock.json"), encoding="utf-8") as fh:
        lock = json.load(fh)
    npm_out = os.path.join(out, "openspec", "npm")
    shutil.rmtree(npm_out, ignore_errors=True)
    os.makedirs(npm_out)
    by_key = {}
    for path, meta in sorted(lock["packages"].items()):
        if not path:
            continue
        if meta.get("dev") or meta.get("optional") or meta.get("link"):
            raise SystemExit("unexpected dev, optional or linked package in the tree: %s" % path)
        name = path.split("node_modules/")[-1]
        key = (name, meta["version"])
        if key not in by_key:
            fname = _npm_file_name(name, meta["version"])
            os.makedirs(os.path.join(dl, "npm"), exist_ok=True)
            src = fetch(meta["resolved"], os.path.join(dl, "npm", fname))
            with open(src, "rb") as fh:
                original = fh.read()
            alg, want = meta["integrity"].split("-", 1)
            if base64.b64encode(hashlib.new(alg, original).digest()).decode() != want:
                raise SystemExit("integrity mismatch for %s" % fname)
            tar_bytes = gzip.decompress(original)
            dst = os.path.join(npm_out, fname)
            stored = store_gzip(tar_bytes, dst, terms, original=original)
            by_key[key] = {"name": name, "version": meta["version"],
                           "license": meta.get("license") or "UNKNOWN",
                           "file": "openspec/npm/" + fname, "sha256": sha256_file(dst),
                           "tar_sha256": hashlib.sha256(tar_bytes).hexdigest(),
                           "integrity": meta["integrity"], "registry_url": meta["resolved"],
                           "stored": stored,
                           "license_member": _license_member(dst), "paths": []}
        by_key[key]["paths"].append(path)
    packages = sorted(by_key.values(), key=lambda p: (p["name"], p["version"]))
    main = [p for p in packages if p["name"] == OPENSPEC["npm_package"]]
    if len(main) != 1 or main[0]["version"] != OPENSPEC["version"]:
        raise SystemExit("the resolved tree does not hold exactly %s" % spec)
    with tarfile.open(os.path.join(out, main[0]["file"])) as tar:
        lic = tar.extractfile("package/LICENSE").read()
        pkg = json.load(tar.extractfile("package/package.json"))
    os.makedirs(os.path.join(out, "openspec"), exist_ok=True)
    with open(os.path.join(out, "openspec", "LICENSE"), "wb") as fh:
        fh.write(lic)
    return {
        "upstream": OPENSPEC["upstream"],
        "license": OPENSPEC["license"],
        "license_file": "openspec/LICENSE",
        "tag": OPENSPEC["tag"],
        "commit": tag_commit(OPENSPEC["upstream"], OPENSPEC["tag"]),
        "source_archive": {"url": url, "sha256": sha256_file(src_archive)},
        "npm_package": OPENSPEC["npm_package"],
        "version": OPENSPEC["version"],
        "engines_node": pkg.get("engines", {}).get("node", ""),
        "entry": posixpath.normpath("node_modules/%s/%s" % (OPENSPEC["npm_package"],
                                                             pkg["bin"]["openspec"])),
        "resolved_with": "npm %s %s %s" % (NPM_TOOL_VERSION, " ".join(NPM_INSTALL), spec),
        "packages": packages,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scratch", required=True, help="a disk-backed scratch directory")
    ap.add_argument("--scan-terms", default=None,
                    help="a term list; stored archives are kept free of matches (see above)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "vendor"))
    args = ap.parse_args(argv)
    scratch = os.path.abspath(args.scratch)
    out = os.path.abspath(args.out)
    os.makedirs(scratch, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    log = os.path.join(scratch, "vendor-commands.log")
    terms = load_scan_terms(args.scan_terms)
    doc = {
        "about": "Pinned upstream copies used by `alpaca spec init`. Rebuild with "
                 "setup/vendor_spec_kits.py; check with `alpaca spec verify`.",
        "spec-kit": vendor_spec_kit(scratch, out, log, terms),
        "openspec": vendor_openspec(scratch, out, log, terms),
    }
    with open(os.path.join(out, "VENDOR.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=False)
        fh.write("\n")
    print("vendor: spec-kit %s (%s), openspec %s (%d packages) -> %s" % (
        doc["spec-kit"]["tag"], doc["spec-kit"]["archive"]["sha256"][:12],
        doc["openspec"]["version"], len(doc["openspec"]["packages"]), out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
