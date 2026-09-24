"""Barrier review 4: each gap the fourth round of probes found, pushed for real where a push is the claim.

The review (docs/alpaca-bootstrap/evidence/review4-barrier-r1/ in the workspace that builds this
repository) names each case N01 to N32. Each test here names the case it covers and drives it: a
real `git push` through the installed hook when the claim is about what reaches the remote, the
library scan or the container reader otherwise.
"""
import gzip
import io
import lzma
import os
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import textwrap
import types
import zipfile
import zlib

import pytest

from alpaca import barrier
from alpaca.gates import containers
from alpaca.gates import leak_audit
from alpaca.gates import verdict as vc
from alpaca.tests.conftest import REPO
from alpaca.tests.test_outbound_barrier import (  # noqa: F401  (fixtures used by name)
    TERM, _commit, _commits, _fixed_clock, _push_env, _run, repo)
from alpaca.tests.test_barrier_review3 import (
    LIST, WORD, _cfg, _payload, _push, _ready, _refused, _remote_has, _remote_sha, _tar, _terms,
    _zip)


def _scan(root):
    return barrier.scan(root, _commits(root))


def _commit_bytes(root, rel, data, msg="vendor a file"):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(data)
    assert _run(root, "add", "-A").returncode == 0
    assert _run(root, "commit", "-q", "-m", msg).returncode == 0
    return _run(root, "rev-parse", "HEAD:" + rel).stdout.strip()


def _hidden(data):
    """The raw bytes do not show the term, so only opening the container can find it."""
    assert TERM.lower().encode() not in data.lower()
    return data


# ------------------------------------------------------------------ N30: a hook older than the pin
LEGACY_HOOK = (
    "#!/bin/sh\n"
    "# alpaca outbound barrier (M4.11): scan every commit entering the remote and refuse\n"
    "# mechanically on a protected path or a sealed term. The decision to push at all is\n"
    "# still a human review card (M3.5); this only stops what must not leave.\n"
    "if [ -x bin/alpaca-python ]; then\n"
    '  exec bin/alpaca-python -m alpaca.barrier prepush "$@"\n'
    "fi\n"
    'exec python3 -m alpaca.barrier prepush "$@"\n')
STALE = "the hook predates the pinned barrier; run bin/alpaca barrier install"


def _legacy_hook(root):
    p = barrier.hook_path(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(LEGACY_HOOK)
    os.chmod(p, 0o755)


def test_n30_a_hook_that_predates_the_pin_refuses_and_says_so(repo):
    root, bare = repo
    _cfg(root)
    _terms(root, LIST)
    _legacy_hook(root)
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    r = _push(root, "main")
    assert r.returncode != 0, r.stdout + r.stderr
    assert STALE in r.stdout + r.stderr
    assert not _remote_has(bare, "refs/heads/main")


def test_n30_the_unpinned_entry_points_refuse(repo, capsys, monkeypatch):
    root, _bare = repo
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert barrier.main(["prepush"]) != vc.PASS
    assert barrier.prepush([], root=root) != vc.PASS
    assert STALE in capsys.readouterr().out


def test_n30_install_replaces_the_old_hook_and_the_push_runs(repo):
    root, bare = repo
    _cfg(root)
    _terms(root, LIST)
    _legacy_hook(root)
    barrier.install(root)
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    r = _push(root, "main")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _remote_has(bare, "refs/heads/main")


# ------------------------------------------------------------------ N07 to N11: container gaps
def _zipapp():
    return b"#!/usr/bin/env python3\n" + _zip([("__main__.py", _payload())])


def _ar(members):
    out = [b"!<arch>\n"]
    for name, data in members:
        out.append(("%-16s%-12d%-6d%-6d%-8o%-10d`\n" % (name + "/", 0, 0, 0, 0o644, len(data)))
                   .encode("ascii"))
        out.append(data + (b"\n" if len(data) % 2 else b""))
    return b"".join(out)


def _deb():
    return _ar([("debian-binary", b"2.0\n"), ("control.tar.xz", lzma.compress(_tar([]))),
                ("data.tar.xz", lzma.compress(_tar([("./usr/share/doc/notes", _payload())])))])


def _orphan_zip():
    """A zip whose first local entry (the term, deflated) is missing from the central directory;
    a clean zip follows it, so a reader of the central directory sees only the clean member."""
    bad = _zip([("notes.md", _payload())])
    local = bad[:bad.index(b"PK\x01\x02")]
    return local + _zip([("clean.md", b"plain words\n")])


def _woff(tables):
    """A WOFF 1.0 font: each table is (tag, stored bytes, declared original length)."""
    head = 44 + 20 * len(tables)
    body, dirs = b"", b""
    for tag, stored, orig in tables:
        dirs += struct.pack(">4sIIII", tag, head + len(body), len(stored), orig, 0)
        body += stored + b"\0" * (-len(stored) % 4)
    total = head + len(body)
    header = struct.pack(">4s4sIHHIHHIIIII", b"wOFF", b"\0\1\0\0", total, len(tables), 0, total,
                         1, 0, 0, 0, 0, 0, 0)
    return header + dirs + body


def _woff_overlong():
    """The table opens to far more than its declared length; the term sits past that length."""
    stream = zlib.compress(b"a" * 20000 + _payload())
    return _woff([(b"name", stream, len(stream) + 1)])


GAPS = {
    "N07-zipapp.pyz": (_zipapp, vc.FAIL),
    "N08-package.deb": (_deb, vc.BLOCKED),
    "N09-notes.lz": (lambda: b"LZIP\x01\x0c" + lzma.compress(_payload(), format=lzma.FORMAT_ALONE)[13:],
                     vc.BLOCKED),
    "N09-notes.lz4": (lambda: b"\x04\x22\x4d\x18" + bytes(range(64)) * 8, vc.BLOCKED),
    "N09-notes.Z": (lambda: b"\x1f\x9d\x90" + bytes(range(64)) * 8, vc.BLOCKED),
    "N09-notes.zz": (lambda: zlib.compress(_payload()), vc.FAIL),
    "N10-orphan.zip": (_orphan_zip, vc.BLOCKED),
    "N11-font.woff": (_woff_overlong, vc.BLOCKED),
}


@pytest.mark.parametrize("name", sorted(GAPS))
def test_n07_to_n11_a_container_with_magic_bytes_is_opened_or_refused(repo, name):
    root, _bare = repo
    _cfg(root)
    _terms(root, LIST)
    make, want = GAPS[name]
    _commit_bytes(root, "vendor/" + name, _hidden(make()))
    res = _scan(root)
    assert res.verdict == want, barrier.render(res.report)


def test_n07_a_real_push_of_a_zipapp_carrying_the_term_is_refused(repo):
    root, bare = repo
    before = _ready(root, bare)
    _commit_bytes(root, "tools/app.pyz", _hidden(_zipapp()))
    _refused(root, bare, before)


def test_n10_a_zip_with_a_stored_nested_zip_is_not_an_orphan(repo):
    """A stored member that is itself a zip carries local headers inside its data: they are content,
    not entries the central directory forgot."""
    root, _bare = repo
    _cfg(root)
    _terms(root, LIST)
    inner = _zip([("a.md", b"plain words\n")])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("inner.zip", inner)
        zf.writestr("b.md", b"more plain words\n")
    _commit_bytes(root, "vendor/outer.zip", buf.getvalue())
    res = _scan(root)
    assert res.verdict == vc.PASS, barrier.render(res.report)


def test_n11_a_woff_table_within_its_declared_length_is_read():
    stream = zlib.compress(_payload())
    font = _hidden(_woff([(b"name", stream, len(_payload()))]))
    names = [(n, d) for n, d in containers.members(font)]
    assert any(TERM.encode() in d for _n, d in names)


def test_a_text_file_that_starts_like_a_zlib_header_is_not_refused(repo):
    root, _bare = repo
    _cfg(root)
    _terms(root, LIST)
    for n, text in enumerate((b"HKEY_LOCAL_MACHINE\\Software\\x = 1\n", b"x^2 + y^2 = r^2\n",
                              b"hC\n", b"x^" + b"plain words and more plain words " * 200)):
        _commit_bytes(root, "notes/t%d.txt" % n, text)
    res = _scan(root)
    assert res.verdict == vc.PASS, barrier.render(res.report)


# ------------------------------------------------------------------ N04, N05: memory stays in the caps
_MEM_CHILD = textwrap.dedent("""
    import resource, sys
    resource.setrlimit(resource.RLIMIT_AS, (3 << 30, 3 << 30))
    from alpaca.gates import containers
    raw = open(sys.argv[1], "rb").read()
    base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    why = "read in full"
    try:
        try:
            it = containers.members(raw, skipped=[])
        except TypeError:
            it = containers.members(raw)
        for _name, _data in it:
            pass
    except containers.Unopenable as e:
        why = "refused: %s" % e
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print("%d %d %s" % (base, peak, why))
""")


def _peak(path):
    env = {**os.environ, "PYTHONPATH": REPO}
    r = subprocess.run([sys.executable, "-c", _MEM_CHILD, path], capture_output=True, text=True,
                       env=env, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    base, peak, why = r.stdout.strip().split(" ", 2)
    return int(base) // 1024, int(peak) // 1024, why


def test_n04_a_zip_bomb_is_refused_within_the_memory_caps(tmp_path):
    p = str(tmp_path / "bomb.zip")
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(12):
            with zf.open("m%02d.bin" % i, "w") as fh:
                for _ in range(60):
                    fh.write(b"\0" * (1 << 20))
    assert os.path.getsize(p) < 2 << 20
    base, peak, why = _peak(p)
    assert why.startswith("refused"), why
    assert peak - base < 400, (base, peak)


def test_n05_a_sparse_tgz_is_refused_within_the_memory_caps(tmp_path):
    if not shutil.which("tar"):
        pytest.skip("needs tar to write sparse members")
    src = tmp_path / "src"
    src.mkdir()
    for i in range(12):
        with open(src / ("s%02d.bin" % i), "wb") as fh:
            fh.truncate(60 << 20)
    p = str(tmp_path / "sparse.tgz")
    r = subprocess.run(["tar", "--sparse", "-czf", p, "-C", str(src), "."], capture_output=True)
    if r.returncode != 0:
        pytest.skip("this tar cannot write sparse members")
    assert os.path.getsize(p) < 1 << 20
    base, peak, why = _peak(p)
    assert why.startswith("refused"), why
    assert peak - base < 400, (base, peak)


class _FakeDecompressor(object):
    """A brotli decompressor that would open to far more than the cap, a step at a time."""
    steps = 0

    def process(self, data, output_buffer_limit=None):
        assert output_buffer_limit, "brotli must run with an output cap"
        _FakeDecompressor.steps += 1
        return b"\0" * output_buffer_limit

    def is_finished(self):
        return False

    def can_accept_more_data(self):
        return False


def _woff2_stub():
    head = struct.pack(">4s4sIHHIII", b"wOF2", b"\0\1\0\0", 100, 1, 0, 100, 10, 0)
    head += struct.pack(">IIIII", 0, 0, 0, 0, 0)
    table = bytes([1]) + bytes([100])                     # tag index 1, original length 100
    return head + table + b"\x8b" * 10


def test_n05_woff2_is_opened_with_a_capped_brotli_decompressor(monkeypatch):
    def whole(_data):
        raise AssertionError("brotli.decompress has no output cap")
    fake = types.SimpleNamespace(decompress=whole, Decompressor=_FakeDecompressor,
                                 error=Exception)
    monkeypatch.setitem(sys.modules, "brotli", fake)
    _FakeDecompressor.steps = 0
    with pytest.raises(containers.Unopenable):
        list(containers.members(_woff2_stub()))
    assert 0 < _FakeDecompressor.steps <= containers.MAX_MEMBER_BYTES // containers._STEP + 2


def test_n05_an_old_brotli_without_an_output_cap_refuses(monkeypatch):
    class Old(object):
        def process(self, data):
            return b""
    fake = types.SimpleNamespace(decompress=lambda d: b"", Decompressor=Old, error=Exception)
    monkeypatch.setitem(sys.modules, "brotli", fake)
    with pytest.raises(containers.Unopenable):
        list(containers.members(_woff2_stub()))


# ------------------------------------------------------------------ N12: a waiver covers only what it names
def _n12_blob():
    stub = b"wOF2" + bytes(range(60))
    return _hidden(gzip.compress(_tar([("package/fonts/a.woff2", stub),
                                       ("package/extra.zip", _zip([("n.md", _payload())]))])))


def test_n12_an_allowed_blob_still_has_its_readable_members_read(repo):
    root, bare = repo
    _cfg(root)
    _terms(root, LIST)
    sha = _commit_bytes(root, "vendor/fonts.tgz", _n12_blob())
    _cfg(root, allow_blobs=["%s  # a font package; its WOFF2 font cannot be opened here" % sha])
    res = _scan(root)
    assert res.verdict == vc.FAIL and barrier.R_LEAK in res.reasons, barrier.render(res.report)


def test_n12_a_real_push_of_the_allowed_package_is_refused(repo):
    root, bare = repo
    _cfg(root)
    _terms(root, LIST)
    sha = _commit_bytes(root, "vendor/fonts.tgz", _n12_blob())
    _cfg(root, allow_blobs=["%s  # a font package; its WOFF2 font cannot be opened here" % sha])
    barrier.install(root)
    r = _push(root, "main")
    assert r.returncode != 0, r.stdout + r.stderr
    assert not _remote_has(bare, "refs/heads/main")


def test_n12_the_report_names_what_the_waiver_did_not_read(repo):
    root, _bare = repo
    _cfg(root)
    _terms(root, LIST)
    stub = b"wOF2" + bytes(range(60))
    clean = gzip.compress(_tar([("package/fonts/a.woff2", stub), ("package/a.md", b"plain\n")]))
    sha = _commit_bytes(root, "vendor/fonts.tgz", clean)
    res = _scan(root)
    assert res.verdict == vc.BLOCKED and barrier.R_UNOPENED in res.reasons
    text = barrier.render(res.report)
    assert "1 part" in text and "fonts" not in text
    _cfg(root, allow_blobs=["%s  # a font package; its WOFF2 font cannot be opened here" % sha])
    res = _scan(root)
    assert res.verdict == vc.PASS, barrier.render(res.report)
    text = barrier.render(res.report)
    assert "barrier.allow_blobs" in text and "1 part" in text


# ------------------------------------------------------------------ N15: a time budget on the scan
def _push_timed(root, *refspec, timeout=120):
    p = subprocess.Popen(["git", "-C", root, "push", "origin", *refspec], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, env=_push_env(root), start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        raise AssertionError("the push hung past %d s" % timeout)
    return p.returncode, out.decode("utf-8", "replace")


def test_n15_a_scan_past_its_time_budget_refuses_with_a_reason(repo):
    root, bare = repo
    before = _ready(root, bare, text=TERM + "\nre: (x+x+)+y\n", time_budget_seconds=3)
    _commit(root, "notes/x.md", "x" * 40 + "\n", "add a note")
    rc, out = _push_timed(root, "main")
    assert rc != 0, out
    assert "time budget" in out and "SCAN-TIME-BUDGET-REFUSES" in out
    assert _remote_sha(bare, "main") == before


def test_n15_an_unusable_time_budget_refuses(repo):
    root, _bare = repo
    _cfg(root, time_budget_seconds="soon")
    _terms(root, LIST)
    barrier.install(root)
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    r = _push(root, "main")
    assert r.returncode != 0 and "time_budget_seconds" in r.stdout + r.stderr


# ------------------------------------------------------------------ N19: a list saved with a BOM
@pytest.mark.parametrize("first", ["re", "include"])
def test_n19_a_list_saved_with_a_bom_keeps_its_first_line(repo, first):
    root, bare = repo
    if first == "re":
        text, carried = "\ufeffre: \\b" + WORD.lower() + "\\b\nZqOtherTokenBeta\n", "see " + WORD
    else:
        _terms(root, TERM + "\n", name="host-terms.txt")
        text, carried = "\ufeff!include: host-terms.txt\nZqOtherTokenBeta\n", "the value is " + TERM
    before = _ready(root, bare, text=text)
    _commit(root, "notes/b.md", carried + "\n", "add a note")
    _refused(root, bare, before)


def test_n19_load_terms_drops_a_leading_bom(tmp_path):
    p = tmp_path / "list.txt"
    p.write_bytes(b"\xef\xbb\xbfre: \\bzqvox\\b\n")
    tl, reasons, _detail = leak_audit.load_terms(str(p))
    assert tl is not None and len(tl.regexes) == 1 and not tl.terms, reasons


# ------------------------------------------------------------------ N25d: no list on a public project
@pytest.mark.parametrize("terms", ["", None])
def test_n25d_a_public_project_with_no_list_refuses(repo, terms):
    root, bare = repo
    _cfg(root, terms=terms)
    _terms(root, LIST)
    barrier.install(root)
    _commit(root, "notes.md", "the value is " + TERM + "\n", "add notes")
    r = _push(root, "main")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "configures no term list" in r.stdout + r.stderr
    assert not _remote_has(bare, "refs/heads/main")


def test_n25d_the_opt_out_still_pushes_with_no_list(repo):
    root, _bare = repo
    _cfg(root, terms="", terms_optional=True)
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    res = _scan(root)
    assert res.verdict == vc.PASS and barrier.R_TERMS_UNCHECKED in res.reasons


def test_n25d_a_private_project_with_no_list_is_not_refused(repo):
    root, _bare = repo
    _cfg(root, terms="")
    with open(os.path.join(root, "project.yaml"), encoding="utf-8") as fh:
        body = fh.read().replace("tier: public", "tier: private")
    with open(os.path.join(root, "project.yaml"), "w", encoding="utf-8") as fh:
        fh.write(body)
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    res = _scan(root)
    assert res.verdict == vc.PASS and barrier.R_TERMS_UNCHECKED in res.reasons


# ------------------------------------------------------------------ N23c: a delete names a ref too
def test_n23c_deleting_a_remote_ref_named_with_a_term_is_refused(repo):
    root, bare = repo
    _ready(root, bare)
    r = _push(root, ":refs/heads/" + TERM.lower())
    assert r.returncode != 0, r.stdout + r.stderr
    assert "ref-name" in r.stdout


def test_n23c_deleting_a_clean_branch_still_passes(repo):
    root, bare = repo
    _ready(root, bare)
    assert _push(root, "main:refs/heads/topic").returncode == 0
    r = _push(root, ":refs/heads/topic")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not _remote_has(bare, "refs/heads/topic")


# ------------------------------------------------------------------ N29: submodules are written down
def test_n29_shipping_names_what_a_submodule_push_skips():
    with open(os.path.join(REPO, "docs", "shipping.md"), encoding="utf-8") as fh:
        text = fh.read()
    part = text[text.index("### What a pre-push hook cannot stop"):]
    assert "--recurse-submodules" in part and "submodule" in part
