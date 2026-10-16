"""Attach mode: Alpaca beside a codebase that other people keep working on (op-062).

The studio (this Alpaca install) holds the record, doctrine and proofs. The existing repository sits
inside it as its own git clone (`attach.repo`, default `app`) and never receives an Alpaca file. Agents
change it only on work branches, and a change leaves the studio only as a branch and a pull request.

A project is in attach mode when project.yaml has an `attach:` block (see config.py). A project
without the block behaves exactly as before: nothing in this package runs for it, and importing the
package does no work beyond defining names.

Modules, each small and owned by one task (design/attach-mode/build-plan.md section 3):
config (the block and its rules), safefs (the one place attach writes to disk), gitops (the one
git runner, and the only way into a task's work folder), sense (gates from files), repo (`attach init`),
health (doctor rows and `attach status`), cli (the `alpaca attach` verb group), baseline (the gate result
per base commit), worktree (the work branch of a task, cut at `task claim`), proven (the one reader of
what attach decides on: plain files under .alpaca/attach/ are output, never read for a decision).
"""


class AttachError(Exception):
    """A refusal. str(exc) is one line and safe to print: no URL userinfo, no token."""


#: the commit identity attach uses in the attached repository when the owner chose none (attach.commit_name and
#: attach.commit_email in project.yaml; build plan P13). It lives here so config and gitops both read one value.
DEFAULT_IDENTITY = ("Alpaca agent", "alpaca-agent@localhost")

#: where attach keeps its files, relative to the studio root (ignored through .git/info/exclude)
STATE_DIR = ".alpaca/attach"
