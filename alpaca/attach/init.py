"""`alpaca attach init ... --card <card>`: attach the repository a setup card names (op-062 t-656, R17, AC-18).

`repo.attach` does the work (clone, ignore proof, gates, block, record). This module adds the first-chat
form: with `--card`, the upstream, base branch, branch prefix and gates come from the card's recorded
inputs, which the owner confirmed with a typed yes, and the options that would name them again are
refused, so the agent cannot retype the setup after the owner said yes to it. Without `--card` it is the
plain attach, with the refusals named by a token (ATTACH-ALREADY, ATTACH-APP-EXISTS, ATTACH-NO-GATES).
The first-chat skill never runs attach without `--card`.
"""
from __future__ import annotations

import os

from alpaca.attach import AttachError, config, gitops, repo

_TOKENS = (("already attached", "ATTACH-ALREADY"), ("already exists in the studio", "ATTACH-APP-EXISTS"),
           ("no attach gates sensed", "ATTACH-NO-GATES"))


def _tag(exc):
    text = str(exc)
    for needle, token in _TOKENS:
        if needle in text and token not in text:
            return AttachError("%s [%s]" % (text, token))
    return exc


def card_inputs(conn, root, card):
    """The recorded settings of a confirmed card, or AttachError. Re-hashes the stored copy."""
    from alpaca import onboard_flow
    confirmed = [r for r in onboard_flow._rows(conn, "onboard-confirmed") if r["data"].get("card") == card]
    if not confirmed:
        raise AttachError("%s is not confirmed by the owner; attach --card needs a card the owner said yes to "
                          "[ATTACH-CARD-NOT-CONFIRMED]" % card)
    if onboard_flow._card_event(conn, card) is None:
        raise AttachError("no card %s on the record [ATTACH-CARD-NOT-CONFIRMED]" % card)
    try:
        return onboard_flow.checked_inputs(conn, root, card)
    except onboard_flow.Refused as exc:
        raise AttachError("%s [ATTACH-CARD-CHANGED]" % exc.detail) from None


def init_verb(conn, root, sid, source, *, card=None, base=None, prefix=None, gates=None, reference=None,
              repo_name="app", answers=None, gates_source=None, upstream=None, pr_footer=None, commit_name=None,
              commit_email=None) -> dict:
    """Attach `source`. Returns {"config", "head", "base"}. Raises AttachError with a token in brackets."""
    root = str(root)
    try:
        if card:
            if (base is not None or prefix is not None or gates is not None or upstream is not None
                    or pr_footer is not None or commit_name is not None or commit_email is not None):
                raise AttachError("with --card the base, branch prefix, gates and upstream come from the card; do not "
                                  "pass them [ATTACH-CARD-FLAGS]")
            inputs = card_inputs(conn, root, card)
            st = inputs["settings"]
            if st.get("mode") != "attach":
                raise AttachError("%s sets the studio up in %s mode, not attach [ATTACH-CARD-MODE]" % (card, st.get("mode")))
            wanted = str(st.get("source") or "")
            if not source or str(source).strip().rstrip("/") != wanted.rstrip("/"):
                raise AttachError("the source %s is not the one %s names (%s) [ATTACH-CARD-SOURCE]"
                                  % (gitops.scrub(source), card, gitops.scrub(wanted)))
            block_gates = [{"name": g["name"], "command": list(g["command"]), "timeout": g.get("timeout", 600)}
                           for g in st["gates"]]
            # the card's pr-says-agent answer reaches the block as pr_footer (op-062 t-690); an identity the owner
            # chose on the card reaches it as commit_name and commit_email (t-689). A card made before these
            # settings existed has neither, and the defaults hold.
            footer = None if st.get("pr_says_agent", True) else False
            cfg = repo.attach(root, wanted, repo=repo_name, base=st["base"], branch_prefix=st["branch_prefix"],
                              gates=block_gates, gates_source="confirmed card %s" % card, answers=answers,
                              session=sid, reference=reference, card=card, pr_footer=footer,
                              commit_name=st.get("commit_name"), commit_email=st.get("commit_email"))
        else:
            cfg = repo.attach(root, source, repo=repo_name, upstream=upstream or config.DEFAULTS["upstream"], base=base,
                              branch_prefix=prefix or config.DEFAULTS["branch_prefix"], gates=gates,
                              gates_source=gates_source, answers=answers, session=sid, reference=reference,
                              pr_footer=pr_footer, commit_name=commit_name, commit_email=commit_email)
    except AttachError as exc:
        raise _tag(exc) from None
    app = str(cfg.repo_path(root))
    head = gitops.git(app, "rev-parse", "--verify", "refs/remotes/%s/%s^{commit}" % (cfg.upstream, cfg.base))
    return {"config": cfg, "head": head, "base": cfg.base}


def _plain_refusal(root):
    """(token, text) when the plain attach (no --card) is refused to this caller, else None. The decision is
    `onboard_flow.setup_write_refusal`, the one gate the flat onboard and `new --attach` use too. A studio with no record
    yet is a fresh one."""
    from alpaca import db, onboard_flow
    if not os.path.isfile(os.path.join(str(root), ".alpaca", "alpaca.db")):
        return onboard_flow.setup_write_refusal(None, root, "attach")
    conn = db.connect(str(root))
    try:
        return onboard_flow.setup_write_refusal(conn, root, "attach")
    finally:
        conn.close()


def run(args, root) -> int:
    """`alpaca attach init SOURCE --card CARD` (and the plain form, with the refusals tagged)."""
    from alpaca import db
    from alpaca.attach.cli import finish
    sid = args.session or "cli"
    # the file arguments are checked first, whoever calls: a pipe, a device or a folder is refused at once by the
    # reader's own text, before the setup gate decides (t-647 follow-up 1). This reads no byte and writes nothing.
    try:
        files = [("answers file", args.answers)]
        if not getattr(args, "card", None):     # with --card a --gates-file is refused as a flag (ATTACH-CARD-FLAGS)
            files.append(("gates file", getattr(args, "gates_file", None)))
        for what, path in files:
            if path is not None:
                repo.check_regular(path, what)
    except AttachError as exc:
        return finish(args, "init", "FAIL", str(exc))
    refused = None if getattr(args, "card", None) else _plain_refusal(root)
    if refused:
        return finish(args, "init", "FAIL", "%s [%s]" % (refused[1], refused[0]))
    conn = db.connect(root) if getattr(args, "card", None) else None   # a plain attach opens no record before its checks
    try:
        gates, gates_source = (None, None) if getattr(args, "card", None) else repo._load_gates(args.gates_file)
        if getattr(args, "card", None):
            flags = [name for name, val, default in (
                ("--gates-file", args.gates_file, None), ("--base", args.base, None),
                ("--branch-prefix", args.branch_prefix, config.DEFAULTS["branch_prefix"]),
                ("--upstream", args.upstream, config.DEFAULTS["upstream"]),
                ("--repo", args.repo, config.DEFAULTS["repo"]),
                ("--no-pr-footer", getattr(args, "no_pr_footer", False), False),
                ("--commit-name", getattr(args, "commit_name", None), None),
                ("--commit-email", getattr(args, "commit_email", None), None)) if val != default]
            if flags:
                raise AttachError("with --card %s come from the card; do not pass them [ATTACH-CARD-FLAGS]"
                                  % ", ".join(flags))
        res = init_verb(conn, root, sid, args.source, card=getattr(args, "card", None),
                        base=None if getattr(args, "card", None) else args.base,
                        prefix=None if getattr(args, "card", None) else args.branch_prefix,
                        gates=gates, answers=args.answers, gates_source=gates_source,
                        upstream=None if getattr(args, "card", None) else args.upstream,
                        repo_name=config.DEFAULTS["repo"] if getattr(args, "card", None) else args.repo,
                        pr_footer=False if getattr(args, "no_pr_footer", False) else None,
                        commit_name=getattr(args, "commit_name", None), commit_email=getattr(args, "commit_email", None))
    except AttachError as exc:
        return finish(args, "init", "FAIL", str(exc))
    cfg = res["config"]
    lines = ["alpaca attach init: %s attached at %s/ (upstream %s, base %s at %s, branch prefix %s)"
             % (cfg.url, cfg.repo, cfg.upstream, cfg.base, res["head"][:7], cfg.branch_prefix)]
    lines += ["  gate %s: %s" % (g["name"], " ".join(g["command"])) for g in cfg.gates]
    receipt = {"repo": cfg.repo, "url": cfg.url, "repository": cfg.repository, "upstream": cfg.upstream,
               "base": cfg.base, "branch_prefix": cfg.branch_prefix, "gates": [g["name"] for g in cfg.gates],
               "pr_footer": cfg.pr_footer, "commit_identity": gitops.identity_label(cfg.identity)}
    return finish(args, "init", "PASS", lines=lines, receipt=receipt)
