"""Fixtures for the setup card (op-062 t-656): five recorded looks with their answers, as plain data.

Each fixture is {"look", "answers", "date", "name", "people"}: the inputs of `onboard_card.render`. They
stand in for what `onboard look` and `onboard answer` record, so the renderer, the situation rules and
the question builder are tested without a clone. `alpaca/tests/fixtures/onboard/<name>.json` holds the
same data and `<name>.card.txt` the card the renderer makes from it; the test fails when they differ.

To change the renderer on purpose, edit it, then run

    python3 -m alpaca.tests.onboardkit --write

read every changed `.card.txt` as the owner would, and commit the new files with the change.
"""
from __future__ import annotations

import json
import os
import sys

from alpaca import onboard_card as oc, onboard_look

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "onboard")

PY_SYNTAX = onboard_look.PY_SYNTAX


def own(value=None, option=None, quote=None, ev=0):
    return {"value": value, "option": option, "source": "owner", "quote": quote, "event": ev}


def agent(value=None, ev=0, **more):
    return dict({"value": value, "source": "agent", "event": ev}, **more)


def _look(**over):
    look = {
        "schema": 1, "now": "2026-10-14", "source_kind": "url",
        "head": {"branch": "main", "sha": "0123456789abcdef0123456789abcdef01234567", "date": "2026-10-01", "subject": "x"},
        "git_safe": True, "git_findings": [],
        "tracked": {"files": 40, "vendor": [], "own": 40, "python_files": 0, "languages": []},
        "build_files": [],
        "history": {"base": "main", "commits": 10, "first": "2026-01-01", "last": "2026-10-01", "days_still": 13,
                    "authors": [{"name": "Author One", "commits": 10}], "recent_authors": [], "tips": []},
        "around": {"ci": [], "hooks": [], "agent_files": [], "codeowners": [], "contributing": None,
                   "contributing_branch_prefix": None, "contributing_branch_rule_at": None, "gitmodules": False, "tests": []},
        "host": {"tools": {"git": "found", "cmake": "found", "python3": "found"}, "wanted": [], "git_user": "Alpaca Development"},
        "gates": [],
    }
    for key, val in over.items():
        look[key] = val
    look["sources"] = onboard_look.sources(look)
    return look


def qtrm64():
    reply1 = ("1a, bach was only testing uart stuff. I want to get it into a clean state first, the build has some weird "
              "stuff and the readme is out of date. later I want to add new features for the beam control. dont touch the "
              "GUI for now")
    goal = ("I want to get it into a clean state first, the build has some weird stuff and the readme is out of date. "
            "later I want to add new features for the beam control")
    opening = ("hi, I took over this firmware project from my old team https://github.com/example-org/QTRM64 . "
               "not sure where to start with alpaca")
    look = _look(
        source="https://github.com/example-org/QTRM64", upstream="https://github.com/example-org/QTRM64",
        repository="example-org/QTRM64",
        head={"branch": "main", "sha": "bd1ef44ec0a17f7020b43b647888d761b87cd7be", "date": "2026-03-24", "subject": "GUI update"},
        tracked={"files": 155, "vendor": [{"dir": "Drivers", "files": 85}], "own": 70, "python_files": 6,
                 "languages": [{"name": "C", "files": 28, "examples": ["Core/Src/main.c", "QTRM-App/Src/gain_phase.c"]},
                               {"name": "Python", "files": 6, "examples": ["qtrm-app.py", "uart_v2/gui.py"]}]},
        build_files=["CMakeLists.txt", "CMakePresets.json", "cmake/gcc-arm-none-eabi.cmake"],
        history={"base": "main", "commits": 57, "first": "2026-01-08", "last": "2026-03-24", "days_still": 204,
                 "authors": [{"name": "Author One", "commits": 19}, {"name": "Author Two", "commits": 16},
                             {"name": "Author Three", "commits": 10}, {"name": "Author Four", "commits": 7},
                             {"name": "Author Five", "commits": 5}],
                 "recent_authors": [{"name": "nguyenbach03022003", "last": "2026-09-11"}],
                 "tips": [{"ref": "origin/dev", "author": "nguyenbach03022003", "date": "2026-09-11"},
                          {"ref": "origin/bachnv32-uart1-1m", "author": "nguyenbach03022003", "date": "2026-06-24"}]},
        host={"tools": {"git": "found", "cmake": "found", "clang": "found", "arm-none-eabi-gcc": None},
              "wanted": ["arm-none-eabi-gcc"], "git_user": "Alpaca Development"},
        gates=[{"name": "py-syntax", "command": list(PY_SYNTAX), "timeout": 120, "kind": "parse", "evidence": "repo:qtrm-app.py",
                "classify": "parse", "trial": {"exit": 0, "seconds": 0.05, "tail": "parsed 6 files"}},
               {"name": "cmake-configure", "command": ["cmake", "-S", ".", "-B", "build/alpaca-gate"], "timeout": 600,
                "kind": "configure", "evidence": "repo:CMakeLists.txt", "classify": "runs-repo-code", "trial": None}])
    answers = {
        "opening": own(opening, quote=opening, ev=2),
        "others-commit": own(None, "a", quote=reply1, ev=4),
        "goal": own(goal, quote=reply1, ev=5),
        "limits": own("dont touch the GUI for now", quote=reply1, ev=6),
        "ci": own(None, "a", quote="1a, there is no CI anywhere, we only ever built it on my laptop", ev=11),
        "done-guess": agent("the CMake build files have no duplicated steps, README.md names only files that exist, and you "
                            "merged each fix on GitHub. Beam-control features come later, as separate work.", ev=7),
        "finding": [agent("The hex and bin step is written twice, so every build runs objcopy twice", ev=8,
                          pointer="repo:CMakeLists.txt:101-112"),
                    agent("README.md names QTRM64.ioc and STM32F429XX_FLASH.ld at the root; neither exists", ev=9,
                          pointer="repo:README.md:31-32")],
        "gate.extra": [agent(None, ev=10, name="arm-syntax", argv=["clang", "--target=arm-none-eabi", "-fsyntax-only",
                                                                  "Core/Src/system_stm32f4xx.c"], timeout=120, kind="parse",
                             classify="parse", reason="the HAL source list in cmake/stm32cubemx/CMakeLists.txt",
                             proves="23 C files type-check against the ST headers for the Cortex-M4. It skips main.c and 6 app "
                                    "files, which need an ARM C library this machine lacks",
                             trial={"exit": 0, "seconds": 0.89, "tail": ""})],
    }
    return {"look": look, "answers": answers, "date": "2026-10-14", "name": "QTRM64",
            "people": [{"name": "Alpaca Development", "role": "owner"}]}


def team():
    opening = "we have a repo at /srv/work/shop-reports, I want to add something"
    reply1 = ("1. add CSV export to the report command. 2b - no wait, Hana the team lead merges, so 2a. 3. feature/<ticket>. "
              "The repo must not get any Alpaca file, and their CI must stay green")
    look = _look(
        source="/srv/work/shop-reports", source_kind="path", upstream="https://github.com/shopco/shop-reports",
        repository="shopco/shop-reports",
        tracked={"files": 41, "vendor": [], "own": 41, "python_files": 22,
                 "languages": [{"name": "Python", "files": 22, "examples": ["report/cli.py", "report/export.py"]}]},
        build_files=["pyproject.toml"],
        history={"base": "main", "commits": 212, "first": "2025-02-03", "last": "2026-10-11", "days_still": 3,
                 "authors": [{"name": "Hana Lead", "commits": 90}, {"name": "Minh Dev", "commits": 70},
                             {"name": "Tuan Dev", "commits": 40}, {"name": "Lan Dev", "commits": 12}],
                 "recent_authors": [{"name": "Hana Lead", "last": "2026-10-11"}, {"name": "Minh Dev", "last": "2026-10-05"},
                                    {"name": "Tuan Dev", "last": "2026-09-27"}, {"name": "Lan Dev", "last": "2026-09-18"}],
                 "tips": [{"ref": "origin/feature/SHOP-12", "author": "Minh Dev", "date": "2026-10-05"}]},
        around={"ci": [".github/workflows/ci.yml"], "hooks": [".husky/pre-push"], "agent_files": ["AGENTS.md"],
                "codeowners": ["CODEOWNERS"], "contributing": "CONTRIBUTING.md", "contributing_branch_prefix": "feature/",
                "contributing_branch_rule_at": "repo:CONTRIBUTING.md:12", "gitmodules": False,
                "tests": ["tests/test_export.py", "tests/test_cli.py"]},
        gates=[{"name": "test", "command": ["python3", "-m", "pytest"], "timeout": 600, "kind": "test",
                "evidence": "repo:pyproject.toml", "classify": "runs-repo-code", "trial": None}])
    answers = {
        "opening": own(opening, quote=opening, ev=2),
        "goal": own("add CSV export to the report command", quote=reply1, ev=4),
        "merges": own("Hana the team lead merges", "a", quote=reply1, ev=5),
        "limits": own("The repo must not get any Alpaca file, and their CI must stay green", quote=reply1, ev=6),
        "done-guess": agent("`report --csv` writes the same rows as the table output, with a test, and Hana merged the pull "
                            "request.", ev=7),
    }
    return {"look": look, "answers": answers, "date": "2026-10-14", "name": "shop-reports",
            "people": [{"name": "Alpaca Development", "role": "owner"}]}


def start_empty():
    opening = "I want to make a little app for my class but I am not a programmer"
    reply1 = ("a page on my laptop that tracks my students' reading logs, working this week. 1a. nothing is published online, "
              "ever, and student names never leave the laptop")
    answers = {
        "opening": own(opening, quote=opening, ev=2),
        "goal": own("a page on my laptop that tracks my students' reading logs, working this week", quote=reply1, ev=3),
        "stack": own(None, "a", quote=reply1, ev=4),
        "limits": own("nothing is published online, ever, and student names never leave the laptop", quote=reply1, ev=5),
        "done-guess": agent("you can type a student's name and the pages they read today, and see a list per student, on your "
                            "laptop, before the end of this week.", ev=6),
    }
    return {"look": None, "answers": answers, "date": "2026-10-14", "name": "class-reading-log",
            "people": [{"name": "Alpaca Development", "role": "owner"}]}


def audit():
    opening = "can you audit this repo for me, I just want to understand how login works before I join the team"
    reply = "I want to know where passwords are checked and whether they are stored safely. 2a"
    look = _look(
        source="https://github.com/acme/webapp", upstream="https://github.com/acme/webapp", repository="acme/webapp",
        tracked={"files": 320, "vendor": [{"dir": "node_modules", "files": 0}], "own": 320, "python_files": 0,
                 "languages": [{"name": "JavaScript", "files": 180, "examples": ["server/index.js", "server/auth.js"]},
                               {"name": "TypeScript", "files": 60, "examples": ["web/app.ts"]}]},
        build_files=["package.json"],
        history={"base": "main", "commits": 640, "first": "2024-01-10", "last": "2026-10-09", "days_still": 5,
                 "authors": [{"name": "Author One", "commits": 400}, {"name": "Author Two", "commits": 240}],
                 "recent_authors": [{"name": "Author One", "last": "2026-10-09"}, {"name": "Author Two", "last": "2026-10-02"}],
                 "tips": []},
        around={"ci": [".github/workflows/test.yml"], "hooks": [], "agent_files": [], "codeowners": [], "contributing": None,
                "contributing_branch_prefix": None, "contributing_branch_rule_at": None, "gitmodules": False,
                "tests": ["server/auth.test.js"]},
        gates=[{"name": "build", "command": ["npm", "run", "build"], "timeout": 600, "kind": "build",
                "evidence": "repo:package.json", "classify": "runs-repo-code", "trial": None},
               {"name": "test", "command": ["npm", "test"], "timeout": 600, "kind": "test", "evidence": "repo:package.json",
                "classify": "runs-repo-code", "trial": None}])
    answers = {
        "opening": own(opening, quote=opening, ev=2),
        "goal": own("where passwords are checked and whether they are stored safely", quote=reply, ev=3),
        "audit-reader": own(None, "a", quote=reply, ev=4),
        "done-guess": agent("a written answer that names the files and lines where login happens and where passwords are "
                            "stored, each with a pointer.", ev=5),
    }
    return {"look": look, "answers": answers, "date": "2026-10-14", "name": "webapp-audit",
            "people": [{"name": "Alpaca Development", "role": "owner"}]}


def migrate_new():
    opening = "we want to rewrite this old PHP shop in Python, in a new repo"
    reply = "2b. a Python version of the same shop with the same pages. the old shop must keep selling until we switch"
    look = _look(
        source="https://github.com/acme/oldshop", upstream="https://github.com/acme/oldshop", repository="acme/oldshop",
        tracked={"files": 210, "vendor": [{"dir": "vendor", "files": 90}], "own": 120, "python_files": 0,
                 "languages": [{"name": "PHP", "files": 100, "examples": ["index.php", "cart.php"]}]},
        build_files=["composer.json"],
        history={"base": "master", "commits": 900, "first": "2015-04-01", "last": "2026-08-30", "days_still": 45,
                 "authors": [{"name": "Author One", "commits": 600}, {"name": "Author Two", "commits": 300}],
                 "recent_authors": [{"name": "Author One", "last": "2026-08-30"}], "tips": []},
        gates=[])
    answers = {
        "opening": own(opening, quote=opening, ev=2),
        "migrate-target": own(None, "b", quote=reply, ev=3),
        "goal": own("a Python version of the same shop with the same pages", quote=reply, ev=4),
        "keep-working": own("the old shop must keep selling until we switch", quote=reply, ev=5),
        "limits": own("none", quote="none", ev=6),
        "done-guess": agent("every page of the old shop has a Python page that shows the same things, checked side by side.", ev=7),
    }
    return {"look": look, "answers": answers, "date": "2026-10-14", "name": "newshop",
            "people": [{"name": "Alpaca Development", "role": "owner"}]}


def qtrm64_long_goal():
    """QTRM64 where the owner wrote much more (t-687, F6-1): the card is longer than 100 lines in all, and every line the
    agent wrote is still within the cap, because the owner's goal and limits do not count toward it."""
    fx = qtrm64()
    goal = (fx["answers"]["goal"]["value"] + ". I also want the uart tool to work on windows, because the people who test the "
            "board use windows laptops and cannot run the linux script, and I want one page in the readme that says how to build "
            "the firmware and how to flash it, written so that a new person can do it on the first day without asking anybody")
    limits = ("dont touch the GUI for now, and dont rename any file under Drivers, and ask me before you change the linker "
              "script or the clock setup, because the board is in the lab and a wrong clock can stop the beam control test")
    fx["answers"]["goal"] = dict(fx["answers"]["goal"], value=goal)
    fx["answers"]["limits"] = dict(fx["answers"]["limits"], value=limits)
    return fx


def qtrm64_gitlab():
    """QTRM64 on a git host that is not GitHub, the owner naming the address in a bracket (t-687, F6-4 and F6-5): parts 6, 8
    and 9 say nothing of GitHub or pull requests, and the situation line quotes only the clause before the bracket."""
    fx = qtrm64()
    url = "https://gitlab.com/example-org/QTRM64"
    fx["look"].update(source=url, upstream=url, repository=None)
    opening = ("hi, I took over this firmware project from my old team (it is at %s). not sure where to start with alpaca" % url)
    fx["answers"]["opening"] = dict(fx["answers"]["opening"], value=opening, quote=opening)
    fx["answers"]["done-guess"] = dict(fx["answers"]["done-guess"], value=(
        "the CMake build files have no duplicated steps and README.md names only files that exist. "
        "Beam-control features come later, as separate work."))
    return fx


def qtrm64_no_trace():
    """QTRM64 where the owner answers the footprint question with no (t-691): the card proposes the trace-free set, a
    neutral commit identity, the pull request footer off and a neutral branch prefix."""
    fx = qtrm64()
    reply = ("1a, 2a. bach was only testing uart stuff. I want to get it into a clean state first, the build has some weird "
             "stuff and the readme is out of date. later I want to add new features for the beam control. dont touch the "
             "GUI for now")
    for slot in ("others-commit", "goal", "limits"):
        fx["answers"][slot] = dict(fx["answers"][slot], quote=reply)
    fx["answers"]["files-in-repo"] = own(None, "a", quote=reply, ev=4)
    return fx


def qtrm64_files_ok():
    """QTRM64 where the owner says Alpaca files may live in the repo and nobody else pushes (t-691): the card proposes
    attach and says the in-place mode is not built in this version."""
    fx = qtrm64_no_trace()
    reply = fx["answers"]["goal"]["quote"].replace("1a, 2a.", "1a, 2b.")
    for slot in ("others-commit", "goal", "limits"):
        fx["answers"][slot] = dict(fx["answers"][slot], quote=reply)
    fx["answers"]["files-in-repo"] = own(None, "b", quote=reply, ev=4)
    return fx


def qtrm64_others():
    """QTRM64 where Bach still pushes on his own branches (t-697): the take-over also asks who merges, and the owner names
    Bach; where the checks run is a CI service the owner names (a Jenkins in the lab), not a file the look found."""
    fx = qtrm64()
    reply2 = "1a, Bach reviews and merges what he pushes, I only approve. 2b, the lab has a Jenkins that builds it nightly"
    reply1 = fx["answers"]["goal"]["quote"].replace("1a, bach was only testing uart stuff.",
                                                    "1b, bach still pushes small fixes on his own branches.")
    for slot in ("goal", "limits"):
        fx["answers"][slot] = dict(fx["answers"][slot], quote=reply1)
    fx["answers"]["others-commit"] = own(None, "b", quote=reply1, ev=4)
    fx["answers"]["merges"] = own("Bach reviews and merges what he pushes", "a", quote=reply2, ev=11)
    fx["answers"]["ci"] = own(None, "b", quote=reply2, ev=12)
    return fx


def team_no_trace():
    """A team repo where the owner wants no trace but names the commit identity and keeps the pull request footer (t-691):
    the owner's own answers win over the proposed set, and the repo's own branch rule wins over the neutral prefix."""
    fx = team()
    reply = ("1. add CSV export to the report command. 2b - no wait, Hana the team lead merges, so 2a. 3a. "
             "Author the commits as Shop Bot <bot@shop.example> and keep the pull request footer on. "
             "The repo must not get any Alpaca file, and their CI must stay green")
    for slot in ("goal", "merges", "limits"):
        fx["answers"][slot] = dict(fx["answers"][slot], quote=reply)
    fx["answers"]["files-in-repo"] = own(None, "a", quote=reply, ev=7)
    fx["answers"]["commit-identity"] = own("Shop Bot <bot@shop.example>", quote=reply, ev=8)
    fx["answers"]["pr-says-agent"] = own("keep the pull request footer on", "a", quote=reply, ev=9)
    return fx


BUILDERS = {"qtrm64-take-over": qtrm64, "team-repo": team, "start-empty": start_empty, "audit": audit,
            "migrate-new-repo": migrate_new, "qtrm64-long-goal": qtrm64_long_goal, "qtrm64-gitlab": qtrm64_gitlab,
            "qtrm64-no-trace": qtrm64_no_trace, "qtrm64-others": qtrm64_others, "qtrm64-files-ok": qtrm64_files_ok, "team-no-trace": team_no_trace}


def render(fixture):
    return oc.render(fixture["look"], fixture["answers"], date=fixture["date"], name=fixture["name"],
                     people=fixture["people"])


def write_all(directory=FIXTURES):
    os.makedirs(directory, exist_ok=True)
    for name, build in BUILDERS.items():
        fx = build()
        with open(os.path.join(directory, name + ".json"), "w", encoding="ascii") as fh:
            json.dump(fx, fh, indent=1, sort_keys=True)
            fh.write("\n")
        with open(os.path.join(directory, name + ".card.txt"), "w", encoding="ascii") as fh:
            fh.write(render(fx))


if __name__ == "__main__":
    if "--write" in sys.argv:
        write_all()
        print("wrote %d fixtures to %s" % (len(BUILDERS), FIXTURES))
    else:
        for name, build in BUILDERS.items():
            print("=" * 20, name)
            print(render(build()))
