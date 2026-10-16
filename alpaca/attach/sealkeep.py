"""What an attach seal keeps, and how (op-062 t-650, fix round 4).

The plain seal (`alpaca/proof.py` `keep_evidence`, `alpaca/research/verify_gate.py` `keep_tree`) writes its
copies by name into `<id>.evidence/`, a folder the agent can prepare before the seal: it makes the folder and
the `.closed` folder below it, writes `.NN-name.part` files and prunes by listing. In a studio with an attached
repository those copies are the product's own code, so a link or a hard link planted at any of those names
carried it out of the studio (rounds 1 to 3: A7, B8, B8b, C2, C2b, C3). Each round guarded the named place and
the next round used the next name.

An attach seal keeps its files differently, and the class has no name left to plant at:

* The kept folder is built **fresh** under a random dot name beside the report
  (`.<id>.evidence.new-<hex>`), by `mkdir` relative to a descriptor of the report folder. Nothing the agent
  prepared can be inside a folder that did not exist a moment ago.
* Every file is written relative to a descriptor (`safefs.Folder`), reached from the real studio root with
  `O_NOFOLLOW` at each part. No path is resolved twice.
* The new folder takes the place of `<id>.evidence` by rename only after every write succeeded. A failure
  before that drops the new folder and nothing else, so a refused seal leaves the latest seal's kept folder as
  that seal recorded it (`proof.seal`: "a refused seal leaves the latest seal's kept directory as that seal
  recorded it"). No code lists or prunes a folder the agent could have prepared; the old one is moved aside
  and removed by a descriptor-based removal that unlinks a link and never follows it.

Placement follows the plain rule (`proof._served_copy`: a copy is served only when the dashboard's file
browser serves its source from where it is) with one more closing: product code of the attached repo is never
served, and in an attach project whose `attach:` block cannot be read the repo folder is recognised by its
name, so a copy of `app/x.py` goes to `.closed` whatever the block says.

`app.patch` is kept as `<id>.evidence/.closed/app.patch`, not beside `tree.patch`: it is the product's diff and
the served folder is the one the browser lists (deviation from build-plan.md:620-626, review K1).

Plain projects never come here; `proof.seal` switches on `proofhook.attach_project`.
"""
from __future__ import annotations

import hashlib
import os

from alpaca.attach import AttachError, STATE_DIR, config, proofhook, safefs


def _refuse(why):
    return ValueError("ATTACH-KEPT %s" % " ".join(str(why).split())[:200])


def maybe_attach(root, real) -> bool:
    """Whether the copy of `real` must be treated as product code of the attached repo. `proofhook.is_attach_path`
    when the `attach:` block is valid; when it is not (an invalid block, or no block but `state.json` says a
    clone was attached), a name test (the block's repo folder, default `app`, and `.alpaca/attach/`), so an
    invalid block never lets product code take a served copy (review K4). A project with neither a block nor
    `state.json` (a leftover `.alpaca/attach/branches/` only) keeps the plain placement."""
    if proofhook.is_attach_path(root, real):
        return True
    try:
        if config.mode(root) is not None:
            return False
        block = config.raw(root)
        if block is config.MISSING and not os.path.lexists(os.path.join(str(root), *STATE_DIR.split("/"), "state.json")):
            return False                          # no block and no attach state: placement is the plain rule
        repo = block.get("repo") if isinstance(block, dict) else None
        if not isinstance(repo, str) or not repo or repo in (".", "..") or "/" in repo or "\\" in repo:
            repo = config.DEFAULTS["repo"]
        base = os.path.realpath(str(root))
        for top in (os.path.join(base, repo), os.path.join(base, *STATE_DIR.split("/"))):
            if real == top or real.startswith(top + os.sep):
                return True
        return False
    except Exception:
        return True


def _read_source(root, target, served, cap):
    """The bytes of the file at `target` as the plain copy reads it (`proof._copy_checked`): the file opened,
    re-checked after the open, not a database by its bytes, at most `cap` bytes. None when any check fails."""
    from alpaca import paths, proof

    def allowed(real):
        return (proof._inside_root(root, real) and proof._not_kept(root, real) is None
                and (not served or (proof._served_copy(root, real) and not maybe_attach(root, real))))

    fh = paths.open_checked(target, allowed)
    if fh is None:
        return None
    with fh:
        first = fh.read(1 << 20)
        if first.startswith(paths.SQLITE_MAGIC):
            return None
        data = first + fh.read(max(0, cap + 1 - len(first)))
    return None if len(data) > cap else data


def keep(root, subj, evidence, taken, app_taken):
    """Keep an attach task's evidence copies and patches in a fresh `<id>.evidence/` and return the seal's
    `tree` value (None when `taken` is None). `evidence` is the list `proof.inspect` returned: each entry
    gains `kept` and `kept_reason` as `proof.keep_evidence` sets them. `taken` is `(head, patch)` of the
    studio tree and `app_taken` `(head, patch, base, tree id)` of the attached repo's work. Raises ValueError
    ("ATTACH-KEPT ...") when a write or the swap fails; the previous kept folder is then untouched."""
    from alpaca import proof
    directory = proof.kept_dir(root, subj)
    final = os.path.basename(directory)
    try:
        with safefs.Folder.at(root, os.path.dirname(directory)) as report:
            return _keep(root, subj, report, directory, evidence, taken, app_taken)
    except AttachError as exc:                    # includes WriteFailed
        raise _refuse(exc) from None
    except OSError as exc:
        raise _refuse("%s: %s" % (final, exc.strerror or exc)) from None


def _keep(root, subj, report, directory, evidence, taken, app_taken):
    from alpaca import proof
    final = os.path.basename(directory)
    seal_name = os.path.basename(proof.report_path(root, subj)) + ".seal.json"
    if report.kind(seal_name) in ("dir",):
        raise _refuse("%s is a folder; the seal copy cannot be written" % seal_name)
    for leftover in (".%s.new-" % final, ".%s.old-" % final):     # of an interrupted seal; best effort only
        try:
            report.drop_prefixed(leftover)
        except AttachError:
            pass                                    # a leftover that cannot be removed must not stop the seal
    fresh_name, fresh = report.fresh_sub(".%s.new-" % final)
    closed = None
    swapped = False
    try:
        kept_names, total = [], 0

        def closed_folder():
            nonlocal closed
            if closed is None:
                closed = fresh.sub(proof.CLOSED_KEPT, exclusive=True)
            return closed

        for pos, item in enumerate(evidence, 1):
            if not isinstance(item, dict):
                continue
            if item.get("kind") != "local":
                item["kept"] = None
                continue
            target = proof._evidence_file(root, "local", item.get("pointer"))
            if target is None or not os.path.isfile(target):
                item["kept"] = None
                item["kept_reason"] = "the file went between resolving it and copying it"
                continue
            withheld = proof._not_kept(root, target)
            if withheld:
                item["kept"] = None
                item["kept_reason"] = withheld
                continue
            size = item.get("bytes") or os.path.getsize(target)
            if size > proof.KEPT_FILE_CAP or total + size > proof.KEPT_TOTAL_CAP:
                item["kept"] = None
                item["kept_reason"] = proof.OVER_CAP
                continue
            name = "%02d-%s" % (pos, proof._slug(os.path.basename(target)))
            served = proof._served_copy(root, target) and not maybe_attach(root, os.path.realpath(target))
            data = _read_source(root, target, served, proof.KEPT_FILE_CAP)
            if data is None or (item.get("sha256") and hashlib.sha256(data).hexdigest() != item["sha256"]):
                item["kept"] = None
                item["kept_reason"] = proof.MOVED_NOT_KEPT
                continue
            (fresh if served else closed_folder()).put(name, data)
            item["kept"] = proof._rel(root, os.path.join(directory, *([] if served else [proof.CLOSED_KEPT]), name))
            kept_names.append(name if served else proof.CLOSED_KEPT + "/" + name)
            total += size
        tree = None
        if taken is not None:
            head, patch = taken
            fresh.put("tree.patch", patch)
            tree = {"head": head, "patch": proof._rel(root, os.path.join(directory, "tree.patch")),
                    "patch_sha256": hashlib.sha256(patch).hexdigest(), "bytes": len(patch)}
            if app_taken is not None:
                head_, patch_, base_, tree_id_ = app_taken
                closed_folder().put("app.patch", patch_)
                tree["app"] = {"head": head_, "base": base_, "tree": tree_id_,
                               "patch": proof._rel(root, os.path.join(directory, proof.CLOSED_KEPT, "app.patch")),
                               "patch_sha256": hashlib.sha256(patch_).hexdigest(), "bytes": len(patch_)}
        if closed is not None:
            closed.close()
        # every write has succeeded: swap the new folder in
        stale = ".%s.old-%s" % (final, fresh_name.rsplit("-", 1)[-1])
        had_old = report.has(final)
        if had_old:
            report.move_entry(final, stale)
        try:
            if kept_names or tree is not None:
                report.move_entry(fresh_name, final)
                swapped = True
        except safefs.WriteFailed:
            if had_old:
                try:
                    report.move_entry(stale, final)
                except safefs.WriteFailed:
                    pass
            raise
        if had_old:
            try:
                report.drop(stale)
            except AttachError:
                pass                                  # the next seal removes the leftover
        return tree
    finally:
        if closed is not None:
            closed.close()
        fresh.close()
        if not swapped:
            try:
                report.drop(fresh_name)
            except AttachError:
                pass


def write_seal_copy(root, report_path, text):
    """`<report>.seal.json`, written through the report folder's descriptor with the bytes and mode
    `util.write_text` gives. Raises OSError when it cannot (the record already holds the seal)."""
    try:
        with safefs.Folder.at(root, os.path.dirname(os.path.abspath(report_path))) as report:
            report.put(os.path.basename(report_path) + ".seal.json", text.encode("utf-8"))
    except AttachError as exc:
        raise OSError(str(exc)) from None
