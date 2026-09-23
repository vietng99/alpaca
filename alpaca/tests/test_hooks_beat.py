import json, os, subprocess, sys
from alpaca import cli, db, project as proj

ENV = lambda project: {**os.environ, "CLAUDE_PROJECT_DIR": project, "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))}

def hook(module, payload, project):
    return subprocess.run([sys.executable, "-m", module], input=json.dumps(payload), capture_output=True,
                          text=True, encoding="utf-8", cwd=project, env=ENV(project))

def test_post_tool_heartbeat(project):
    cli.main(["init"])
    p = hook("alpaca.hooks.post_tool", {"session_id": "s1", "tool_name": "Edit", "tool_input": {"file_path": "/x/y.py"}}, project)
    assert p.returncode == 0 and p.stdout == ""
    conn = db.connect(project)
    e = db.events(conn, kind="heartbeat")[-1]
    assert e["data"] == {"tool": "Edit", "ref": "/x/y.py"}
    assert db.rows(conn, "sessions", "sid='s1'")[0]["beats"] == 1

def test_post_tool_redacts_bash_command(project):
    cli.main(["init"])
    cmd = 'curl -H "Authorization: Bearer sk-live-SECRET"'
    p = hook("alpaca.hooks.post_tool", {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": cmd}}, project)
    assert p.returncode == 0 and p.stdout == ""
    conn = db.connect(project)
    e = db.events(conn, kind="heartbeat")[-1]
    assert e["data"]["ref"].startswith("curl sha256:")
    assert "SECRET" not in e["data"]["ref"]

def test_user_prompt_reminder_every_fifth(project):
    cli.main(["init"])
    outs = [hook("alpaca.hooks.user_prompt", {"session_id": "s1", "prompt": "hi"}, project).stdout for _ in range(5)]
    assert outs[0] == "" and outs[4] != ""
    assert "next:" in json.loads(outs[4])["hookSpecificOutput"]["additionalContext"]

def test_user_prompt_injects_banned_list_when_present(project):
    cli.main(["init"])
    with open(os.path.join(project, "style", "banned.txt"), "w", encoding="utf-8") as f:
        f.write("synergy\n")
    out = hook("alpaca.hooks.user_prompt", {"session_id": "s2", "prompt": "x"}, project).stdout
    assert "synergy" in json.loads(out)["hookSpecificOutput"]["additionalContext"]

def test_user_prompt_parses_preset_words_and_phrases_only(project):
    cli.main(["init"])
    os.makedirs(os.path.join(project, "style", "presets"), exist_ok=True)
    with open(os.path.join(project, "style", "presets", "tiny-ban-list.md"), "w", encoding="utf-8") as f:
        f.write("""# tiny
## Application rules
- Apply the word and phrase bans to your own prose.
## 1. Words
### A-C
- synergy
- `green`: vague praise
## 2. Phrases
- Great question
## 3. Sentence structures
- Avoid: `Not X, but Y.`
- Instead: state the distinction.
""")
    proj.save(project, {"name": "d", "style": {"presets": ["tiny"]}})
    out = hook("alpaca.hooks.user_prompt", {"session_id": "s3", "prompt": "x"}, project).stdout
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "synergy" in ctx and "Great question" in ctx
    assert "Apply the word" not in ctx and "Not X, but Y" not in ctx
