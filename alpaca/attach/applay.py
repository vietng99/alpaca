"""What `alpaca verify` does with the attached repository's bytes (op-062 t-650, fix round 4).

The verifier copies the kept `app.patch` into the verification folder, lays the attached repo's tree at the
sealed base into each try's `tree/app` and `base/app`, and copies `app/` evidence into each try's packet. The
verification folder `.alpaca/verify/` is agent-writable (the verifier says so itself), so a link planted at a
name the verifier writes carried product code out of the studio (S2, K2, K3, P3, P4, P5). Every one of those
writes now goes through `safefs.Folder`: reached from the real studio root with `O_NOFOLLOW` at each part,
relative to a descriptor, into a folder made in the same call or a name that is replaced, never written
through. The kept patch is read once, bounded, and given to `git apply` on stdin, so nothing re-reads the file
by name between the hash check and the apply.

Plain projects never come here; `alpaca/research/verifier.py` calls this module only for a seal that carries
an app patch and for an attach project's evidence copies.
"""
from __future__ import annotations

import hashlib
import os

from alpaca.attach import AttachError, safefs
from alpaca.attach.proofhook import attach_project  # noqa: F401  (re-exported for the verifier)


def _inside(root):
    from alpaca import proof
    return lambda real: proof._inside_root(root, real)


def _kept_path(root, rel):
    rel = "/".join(str(rel or "").split("\\")).strip()
    if not rel or os.path.isabs(rel) or ".." in rel.split("/"):
        return None
    return os.path.join(str(root), *rel.split("/"))


def copy_patch(root, kept_rel, want_sha, vf_dir) -> bool:
    """Copy the kept app patch at `kept_rel` to `<vf_dir>/app.patch`, only when its bytes hash to `want_sha` and
    are not over the kept cap. True when it was copied. The file is read with no link followed and written by
    descriptor: a link already at the name is replaced and its target is left alone (S2)."""
    from alpaca import paths, proof
    source = _kept_path(root, kept_rel)
    if source is None:
        return False
    fh = paths.open_checked(source, _inside(root))
    if fh is None:
        return False
    with fh, safefs.Folder.at(root, vf_dir, create=False) as folder:
        return folder.put_stream("app.patch", fh, want_sha=want_sha, limit=proof.KEPT_FILE_CAP) is not None


def read_patch(root, vf_dir, want_sha):
    """The bytes of `<vf_dir>/app.patch` when they hash to `want_sha` (one bounded read, no link followed),
    else None. The caller applies these bytes, not the file."""
    from alpaca import paths, proof
    fh = paths.open_checked(os.path.join(str(vf_dir), "app.patch"), _inside(root))
    if fh is None:
        return None
    with fh:
        data = fh.read(proof.KEPT_FILE_CAP + 1)
    if len(data) > proof.KEPT_FILE_CAP or hashlib.sha256(data).hexdigest() != want_sha:
        return None
    return data


def lay(root, dest, entries, blobs) -> None:
    """Write the attached repo's tree into `dest`, a folder that must not exist yet: regular files first (the
    executable bit kept), then links. `entries` is [(mode, sha, path)] and `blobs` {sha: bytes}. `dest` is made
    exclusively by descriptor, so a link planted there, or a studio commit that tracks `app` as a link, makes
    the mkdir fail with nothing written (K2, K3); every file below it is made by descriptor in a folder made
    in this call. A path below a link, or named twice, raises AttachError."""
    dest = os.fspath(dest)
    name = os.path.basename(dest)
    with safefs.Folder.at(root, os.path.dirname(dest), create=False) as parent:
        try:
            top = parent.sub(name, exclusive=True)
        except FileExistsError:
            raise AttachError("the studio tree already holds %s/" % name) from None
        with top:
            for want_links in (False, True):
                _lay_pass(top, entries, blobs, links=want_links)


def _lay_pass(top, entries, blobs, *, links):
    stack = []                                   # [(part, Folder)] for the folder of the previous entry

    def folder_for(parts, path):
        keep = 0
        while keep < min(len(stack), len(parts)) and stack[keep][0] == parts[keep]:
            keep += 1
        while len(stack) > keep:
            stack.pop()[1].close()
        for part in parts[keep:]:
            here = stack[-1][1] if stack else top
            try:
                stack.append((part, here.sub(part, create=True)))
            except AttachError as exc:
                if "is a link" in str(exc):
                    raise AttachError("the base tree names a path below a link: %s" % path) from None
                raise AttachError("the base tree names a path twice: %s" % path) from None
        return stack[-1][1] if stack else top

    try:
        for mode, sha, path in sorted(entries, key=lambda e: e[2]):
            if (mode == "120000") != links:
                continue
            parts = path.split("/")
            folder = folder_for(parts[:-1], path)
            try:
                if links:
                    folder.add_link(parts[-1], blobs[sha].decode("utf-8", "surrogateescape"))
                else:
                    folder.put_new(parts[-1], blobs[sha], mode=safefs.masked(0o755 if mode == "100755" else 0o644))
            except AttachError as exc:
                if "already exists" in str(exc):
                    raise AttachError("the base tree names a path twice: %s" % path) from None
                raise
    finally:
        while stack:
            stack.pop()[1].close()


def place_copy(root, sources, want_sha, target) -> bool:
    """Copy the first of `sources` (paths) whose bytes hash to `want_sha` to `target`, which lies below the
    studio root: the file is read with no link followed and written by descriptor into folders walked from the
    real root, so a link planted at a folder of the verification (P3, P4) or at the file name (P5) takes no
    bytes out of the studio. False when no source matches."""
    target = os.fspath(target)
    for cand in sources:
        fh = _open_source(root, cand)
        if fh is None:
            continue
        with fh:
            with safefs.Folder.at(root, os.path.dirname(target), create=True) as folder:
                if folder.put_stream(os.path.basename(target), fh, want_sha=want_sha) is not None:
                    return True
    return False


def _open_source(root, cand):
    from alpaca import paths
    return paths.open_checked(cand, _inside(root))
