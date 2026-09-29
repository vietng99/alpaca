#!/usr/bin/env python3
"""Render README GIFs from the editable SVGs.

Optional artwork tools only: pip install CairoSVG Pillow
Run from any directory: python setup/render_readme_assets.py
Uses system Arial-compatible sans and DejaVu Sans Mono fonts.
"""
from io import BytesIO
from pathlib import Path
from html import escape
import argparse
import json
import math
import textwrap
import xml.etree.ElementTree as ET

import cairosvg
from PIL import Image


ASSETS = Path(__file__).resolve().parents[1] / "docs" / "assets"


def frame(source, changes):
    root = ET.fromstring(source)
    for element in root.iter():
        element.attrib.update(changes.get(element.get("id"), {}))
    rendered = cairosvg.svg2png(bytestring=ET.tostring(root), scale=1.5)
    rgba = Image.open(BytesIO(rendered)).convert("RGBA")
    # GIF has one-bit transparency; flatten edges onto the warm page color.
    background = Image.new("RGB", rgba.size, "#f7f5ed")
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background.resize((int(root.get("width")), int(root.get("height"))), Image.Resampling.LANCZOS)


def save(name, frames, durations, palette_source=None):
    # A shared palette prevents stationary artwork from shimmering across frames.
    palette = (palette_source or frames[-1]).quantize(colors=128)
    indexed = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    target = ASSETS / (name + ".gif")
    indexed[0].save(target, save_all=True, append_images=indexed[1:],
                    duration=durations, loop=0, optimize=True, disposal=1)
    print(f"{target.name}: {len(frames)} frames, {sum(durations) / 1000:g}s, {target.stat().st_size:,} bytes")


def banner():
    source = (ASSETS / "banner.svg").read_text(encoding="utf-8")
    frames = []
    # Quiet eight-second loop: relaxed lids, one slow blink, barely moving scarf.
    for index in range(64):
        phase = index / 64 * math.tau
        blink = {27: 0.75, 28: 0.4, 29: 0.1, 30: 0.1, 31: 0.4, 32: 0.75}.get(index, 1)
        frames.append(frame(source, {
            "eyes": {"transform": f"translate(0 114) scale(1 {blink}) translate(0 -114)"},
            "scarf-tail": {"transform": f"rotate({0.45 * math.sin(phase):.3f} 195 164)"},
        }))
    save("banner", frames, [120, 130] * 32)


def terminal():
    source = (ASSETS / "terminal.svg").read_text(encoding="utf-8")
    states = [
        ({"seal": {"opacity": "0"}, "close": {"opacity": "0"}, "progress": {"opacity": "0"}}, 2200),
        ({"close": {"opacity": "0"}, "progress": {"opacity": "0"}}, 2200),
        ({}, 4200),
    ]
    save("terminal", [frame(source, changes) for changes, _ in states], [duration for _, duration in states])


def workflow_svg(story, scene):
    """Keep every stage visible while the central artifact changes."""
    colors = {"text": "#e7ece1", "muted": "#9bad9c", "accent": "#e4c584",
              "good": "#b4d7a4", "warn": "#f2b194"}
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="790" viewBox="0 0 1200 790" role="img" aria-labelledby="title desc">',
             '<title id="title">From raw idea to spec, runbook, checklist, agent work and proof report.</title>',
             '<desc id="desc">Illustrative link-shortener workflow. Follow SC-002 through a failed latency check, a bounded retry, passing evidence and a sealed proof. Release remains an owner decision.</desc>',
             '<rect width="1200" height="790" rx="22" fill="#15271f"/>']

    def rect(x, y, w, h, fill, radius=10, stroke=None):
        extra = f' stroke="{stroke}"' if stroke else ''
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}"{extra}/>')

    def text(x, y, value, size=18, color="#e7ece1", weight="400", mono=False):
        family = "DejaVu Sans Mono, monospace" if mono else "Arial, Helvetica, sans-serif"
        space = ' xml:space="preserve"' if mono else ''
        parts.append(f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="{weight}" fill="{color}"{space}>{escape(value)}</text>')

    text(40, 45, story["subtitle"], 12, "#acc59f", "700")
    text(40, 96, story["title"], 39, weight="700")
    text(40, 127, "A link-shortener walkthrough / illustrative artifacts and results", 16, "#9bad9c")
    step = scene["step"]
    for index, label in enumerate(story["steps"]):
        x = 40 + index * 190
        active, passed = index == step, index < step
        rect(x, 161, 170, 53, "#dce8cf" if active else "#223d2d" if passed else "#1c3026", 9)
        color = "#213e32" if active else "#b4d7a4" if passed else "#9bad9c"
        text(x + 13, 194, f"{index + 1:02}", 13, color, "700", True)
        text(x + 41, 194, label, 16, color, "700" if active else "400")
        if index < 5:
            text(x + 177, 193, ">", 15, "#78966b" if passed else "#49604e")

    rect(40, 239, 730, 413, "#102017", 12, "#35523c")
    text(62, 270, scene["file"], 14, "#acbca7", mono=True)
    parts.append('<path d="M40 286H770" stroke="#35523c"/>')
    text(62, 324, scene["heading"], 23, weight="700")
    for index, (kind, line) in enumerate(scene["lines"]):
        text(62, 358 + index * 26, line, 16, colors[kind], mono=True)

    rect(790, 239, 370, 413, "#f1f1e5", 12)
    text(814, 272, "THE AGENT'S NEXT MOVE", 12, "#60755c", "700")
    title_lines = textwrap.wrap(scene["action"], 24)
    for index, line in enumerate(title_lines):
        text(814, 310 + index * 29, line, 25, "#213e32", "700")
    for index, line in enumerate(textwrap.wrap(scene["body"], 34)):
        text(814, 366 + index * 25, line, 18, "#4e644b")
    for index, line in enumerate(scene["facts"]):
        text(814, 507 + index * 30, line, 14, "#355944", mono=True)
    status_color = "#8b482d" if scene["status"] in ("FAIL", "RETRY") else "#355944"
    rect(814, 600, 322, 30, "#e3e8d8", 5)
    text(830, 621, scene["status"], 13, status_color, "700", True)

    rect(40, 673, 1120, 54, "#263e2d", 8)
    text(60, 707, scene["trace"], 18, "#d1dfbf")
    text(40, 761, scene["footer"], 15, "#a9b9a2")
    parts.append('</svg>')
    return "\n".join(parts) + "\n"


def workflow():
    story = json.loads((ASSETS / "workflow.json").read_text(encoding="utf-8"))
    scenes = story["scenes"]
    frames = [frame(workflow_svg(story, scene), {}) for scene in scenes]
    # Sample every scene so the FAIL/RETRY colors survive the shared GIF palette.
    palette_source = Image.new("RGB", (300 * len(frames), 198))
    for index, picture in enumerate(frames):
        palette_source.paste(picture.resize((300, 198)), (index * 300, 0))
    save("workflow", frames, [scene["duration"] for scene in scenes], palette_source)
    (ASSETS / "workflow.svg").write_text(workflow_svg(story, scenes[-1]), encoding="utf-8")


def hub_svg(story, scene):
    """A small illustrative stage map, using the hub's distinct state colors."""
    states = {
        "pass": ("#e8f3eb", "#217451", "PASS", "PROOF SEALED"),
        "live": ("#edf0ff", "#4b58cc", "LIVE", "RUNNING NOW"),
        "waiting": ("#fafbf8", "#bdc9c0", "NO RUN", "NOT STARTED"),
        "fail": ("#fff0ed", "#b4483b", "FAIL", "FAILED ATTEMPT"),
        "paused": ("#fcf1dd", "#956c25", "PAUSED", "OWNER CHECK-IN"),
    }
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="620" viewBox="0 0 1200 620" role="img" aria-labelledby="title desc">',
             '<title id="title">Watch the work move through the hub.</title>',
             '<desc id="desc">Simplified illustrative hub: five connected stages and their task cards. Blue is live, green is passing work, red is a failed attempt and amber is an owner check-in. Release runs only after an explicit owner approval.</desc>',
             '<rect width="1200" height="620" rx="20" fill="#f7f8f3"/>']

    def text(x, y, value, size=16, color="#273e35", weight="400", mono=False):
        family = "DejaVu Sans Mono, monospace" if mono else "Arial, Helvetica, sans-serif"
        parts.append(f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="{weight}" fill="{color}">{escape(value)}</text>')

    def card(x, y, title, state, task=False):
        fill, color, stage_label, task_label = states[state]
        dashed = ' stroke-dasharray="5 4"' if state == "waiting" else ''
        parts.append(f'<rect x="{x}" y="{y}" width="208" height="92" rx="8" fill="{fill}" stroke="{color}" stroke-width="1.5"{dashed}/>')
        parts.append(f'<rect x="{x}" y="{y + 1}" width="4" height="90" rx="2" fill="{color}"/>')
        text(x + 14, y + 31, title, 18, weight="600")
        label_color = "#64756b" if state == "waiting" else color
        text(x + 14, y + 72, task_label if task else stage_label, 12, label_color, mono=True)
        if state == "live":
            parts.append(f'<circle cx="{x + 188}" cy="{y + 24}" r="4" fill="{color}"/>')

    text(40, 39, "ALPACA / OPERATIONS HUB", 12, "#52745e", "700", True)
    text(40, 84, "Watch the work move.", 36, weight="700")
    text(40, 115, "Stages above. Tasks and their proof below. One shared record.", 18, "#657568")
    done = scene["states"].count("pass")
    text(943, 46, f"{done} / 5 stages passed", 16, "#217451", "600")
    text(943, 74, scene["owner"], 13, "#7f693c")
    for x, width, label in ((40, 436, "PLAN"), (496, 436, "EXECUTE"), (952, 208, "OWNER GATE")):
        text(x + 7, 151, label, 11, "#657568", "700", True)
        parts.append(f'<path d="M{x} 169v-9h{width}v9" fill="none" stroke="#d2ddd2" stroke-width="1.5"/>')
    for i, (stage, task, state) in enumerate(zip(story["stages"], story["tasks"], scene["states"])):
        x = 40 + i * 228
        card(x, 180, stage, state)
        if i < 4:
            parts.append(f'<path d="M{x + 209} 226h15m-5-4 5 4-5 4" fill="none" stroke="#8d9c91" stroke-width="1.5"/>')
        parts.append(f'<path d="M{x + 104} 281v41m-4-5 4 5 4-5" fill="none" stroke="#b3c1b6" stroke-width="1.3" stroke-dasharray="3 4"/>')
        card(x, 333, task, state, task=True)
    parts.append('<rect x="40" y="447" width="1120" height="82" rx="8" fill="#edf1e8"/>')
    text(58, 478, scene["event"], 17, "#355441", "600")
    text(58, 507, scene["history"], 15, "#667365")
    for x, color, label in ((40, "#217451", "PASS / SEALED"), (266, "#4b58cc", "LIVE"), (415, "#b4483b", "FAILED"), (590, "#956c25", "CHECK-IN"), (791, "#bdc9c0", "NOT STARTED")):
        parts.append(f'<circle cx="{x + 5}" cy="564" r="5" fill="{color}"/>')
        text(x + 19, 568, label, 12, "#617164", mono=True)
    text(40, 603, "Simplified illustrative hub view. Stage layouts come from the project's profile; this README animation uses sample states.", 12, "#788273")
    parts.append('</svg>')
    return "\n".join(parts) + "\n"


def hub():
    story = json.loads((ASSETS / "hub.json").read_text(encoding="utf-8"))
    scenes = story["scenes"]
    frames = [frame(hub_svg(story, scene), {}) for scene in scenes]
    palette_source = Image.new("RGB", (300 * len(frames), 155))
    for index, picture in enumerate(frames):
        palette_source.paste(picture.resize((300, 155)), (index * 300, 0))
    save("hub", frames, [scene["duration"] for scene in scenes], palette_source)
    # Keep the owner check-in visible in the reduced-motion still.
    (ASSETS / "hub.svg").write_text(hub_svg(story, scenes[5]), encoding="utf-8")


def hub_tour_svg(story, scene):
    """A feature tour with invented examples, not a reproduction of private hub data."""
    ink, muted, green, blue = "#243e33", "#65766d", "#217451", "#4b58cc"
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="740" viewBox="0 0 1200 740" role="img" aria-labelledby="title desc">',
             f'<title id="title">Alpaca hub tour: {escape(scene["label"])}</title>',
             f'<desc id="desc">Simplified feature illustration with invented sample data. {escape(scene["subtitle"])} {escape(scene["takeaway"])}</desc>']

    def rect(x, y, w, h, fill="#ffffff", stroke=None, radius=10):
        extra = f' stroke="{stroke}"' if stroke else ''
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}"{extra}/>')

    def text(x, y, value, size=18, color=ink, weight="400", mono=False):
        family = "DejaVu Sans Mono, monospace" if mono else "Arial, Helvetica, sans-serif"
        parts.append(f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="{weight}" fill="{color}">{escape(str(value))}</text>')

    def line(x1, y1, x2, y2, color="#dce4dc"):
        parts.append(f'<path d="M{x1} {y1}L{x2} {y2}" stroke="{color}" fill="none"/>')

    def dot(x, y, color, radius=5):
        parts.append(f'<circle cx="{x}" cy="{y}" r="{radius}" fill="{color}"/>')

    rect(0, 0, 1200, 740, "#f7f8f3", radius=20)
    text(32, 39, "ALPACA / HUB + PROJECT MEMORY", 12, green, "700", True)
    text(32, 88, story["title"], 36, weight="700")
    text(32, 120, "Follow autonomous progress, then inspect the record behind it.", 19, muted)
    line(32, 143, 1168, 143)
    text(32, 181, "FEATURE TOUR", 12, muted, "700", True)
    nav_pitch = min(78, 420 / len(story["scenes"]))
    for i, item in enumerate(story["scenes"]):
        y = 196 + i * nav_pitch
        active = item["id"] == scene["id"]
        if active:
            rect(32, y, 240, min(66, nav_pitch - 9), "#e5ede0")
            rect(32, y + 8, 4, min(46, nav_pitch - 25), green, radius=2)
        text(49, y + 24, f"{i + 1:02}", 13, green if active else muted, "700", True)
        text(82, y + 24, item["label"], 17, weight="700" if active else "400")
        text(82, y + 44, item["hint"], 13, muted)
    text(48, 624, "ONE SHARED RECORD", 12, muted, "700", True)

    rect(296, 169, 872, 469, stroke="#dce4dc", radius=14)
    text(324, 211, scene["heading"], 28, weight="700")
    text(324, 242, scene["subtitle"], 18, muted)
    key = scene["id"]
    if key == "sessions":
        dot(330, 282, green)
        text(345, 288, "2 active project sessions", 17, green, "600")
        text(900, 288, "1 idle in recent window", 16, muted)
        cards = [(324, "Implementer", "3s ago / Edit", "03", "Build redirect handler", "Keep redirects below 100 ms.", "Implement the agreed contract."),
                 (742, "Reviewer", "9s ago / Shell", "04", "Check edge cases", "Check timeout and retry cases.", "Record the failures and proof.")]
        for x, title, beat, number, task, ask1, ask2 in cards:
            rect(x, 310, 398, 242, "#f7f9f4", "#dce4dc")
            dot(x + 20, 336, green)
            text(x + 35, 343, title, 22, weight="600")
            text(x + 18, 372, "Heartbeat " + beat, 15, muted)
            line(x + 18, 386, x + 380, 386)
            text(x + 18, 412, "TASK " + number + " / DOING", 12, blue, "700", True)
            text(x + 18, 441, task, 21, weight="600")
            text(x + 18, 477, "LATEST CAPTURED ASK", 11, muted, "700", True)
            text(x + 18, 502, ask1, 17)
            text(x + 18, 527, ask2, 17)
        text(324, 584, "Unclaimed work is flagged. Open a session to inspect its conversation.", 17, muted)
        text(324, 614, "Agent crew: captured parent + child conversations, each with its own scope.", 16, green)
    elif key == "work":
        text(324, 285, "3 sealed", 16, green, "700")
        text(462, 285, "1 running", 16, blue, "700")
        text(613, 285, "1 open", 16, muted)
        text(1030, 285, "5 tasks", 16, muted)
        rect(324, 301, 816, 10, "#e5eae4", radius=5)
        rect(324, 301, 490, 10, green, radius=5)
        rect(819, 301, 158, 10, blue, radius=0)
        rows = [("03", "Cover edge cases", "Done", "Result recorded / proof linked", green, "#edf5ee"),
                ("04", "Check redirect latency", "Doing", "Current attempt / method recorded", blue, "#eff1ff"),
                ("05", "Release + health check", "Open", "Owner gate / not started", muted, "#f6f8f4")]
        for i, (number, title, status, detail, color, fill) in enumerate(rows):
            y = 330 + i * 82
            rect(324, y, 816, 71, fill)
            rect(324, y + 8, 4, 55, color, radius=2)
            text(343, y + 31, number, 15, color, mono=True)
            text(384, y + 29, title, 21, weight="600")
            text(384, y + 53, detail, 15, muted)
            text(1047, y + 30, status, 17, color, "700")
        text(324, 605, "Open the report  >  inspect evidence  >  reproduce the result", 17, green)
    elif key == "runs":
        for x, label in ((324, "COMPONENT"), (518, "LINT"), (735, "TEST"), (952, "BUILD")):
            text(x, 284, label, 12, muted, "700", True)
        for i, (name, states) in enumerate((("API", ["PASS"] * 3), ("Worker", ["PASS", "LIVE", "NO RUN"]), ("Web", ["PASS"] * 3))):
            y = 302 + i * 63
            text(324, y + 31, name, 19, weight="600")
            for j, state in enumerate(states):
                x = 500 + j * 217
                color, fill = (green, "#e8f3eb") if state == "PASS" else (blue, "#edf0ff") if state == "LIVE" else (muted, "#f7f8f5")
                rect(x, y, 198, 48, fill, color if state != "NO RUN" else "#dce4dc", 7)
                text(x + 18, y + 30, state, 17, color, "600")
        rect(324, 510, 816, 100, "#edf0ff")
        dot(345, 535, blue)
        text(362, 541, "Following worker / tests", 18, blue, "700")
        text(343, 570, "[live] integration checks in progress", 16, ink, mono=True)
        text(343, 595, "Pause following / filter lines / save loaded output", 14, muted)
    elif key == "live":
        dot(330, 282, blue)
        text(345, 288, "2 jobs running", 17, blue, "600")
        text(884, 288, "Auto layout / Pause all", 16, muted)
        jobs = [(324, "API / tests", "[12:04] integration checks", "[12:05] case 8 of 12", "[12:06] checking retries", "0 errors / 1 warning", "384 MB RSS"),
                (742, "Web / build", "[12:04] compiling modules", "[12:05] bundling assets", "[12:06] writing output", "0 errors / 0 warnings", "256 MB RSS")]
        for x, title, a, b, c, counts, memory in jobs:
            rect(x, 310, 398, 250, "#f6f8f3", "#dce4dc")
            text(x + 18, 344, title, 22, weight="600")
            text(x + 18, 371, "Pause / Focus / Open full run", 14, blue)
            rect(x + 10, 389, 378, 116, "#172b22", radius=6)
            for i, value in enumerate((a, b, c)):
                text(x + 24, 420 + i * 29, value, 16, "#b8cbb7", mono=True)
            text(x + 18, 531, counts, 15, muted)
            text(x + 262, 531, memory, 14, muted)
        text(324, 594, "Tiles follow stage changes; finished jobs can stay pinned for comparison.", 17, muted)
        text(324, 619, "Jobs and sampled process memory come from the configured project profile.", 15, muted)
    elif key == "logs":
        rect(324, 271, 816, 153, "#172b22")
        log = [("042  INFO   Running latency checks", "#b8cbb7"),
               ("043  WARN   Slow response detected", "#e9c989"),
               ("044  ERROR  Expected p95 < 100 ms", "#f1b2a5"),
               ("045  ERROR  Observed p95 = 127 ms", "#f1b2a5")]
        for i, (value, color) in enumerate(log):
            text(344, 303 + i * 30, value, 19, color, mono=True)
        text(324, 452, "PROFILE-PROVIDED AGENT REVIEW", 12, muted, "700", True)
        rect(324, 467, 816, 143, "#faf4e6")
        text(344, 496, "The quoted result exceeds the latency limit.", 21, weight="600")
        text(344, 526, "Lines 044-045 / quotes matched to the recorded log", 17, muted)
        text(344, 555, "Check the slow path before the next attempt.", 18)
        rect(344, 570, 178, 26, "#f0e2c5", radius=5)
        text(354, 588, "Not checked by engineer", 13, "#866322")
    elif key == "wiki":
        for i, (title, detail) in enumerate((("Capture", "Session events"), ("Keep", "Project-local vault"), ("Trace", "Source pointers"))):
            x = 324 + i * 280
            rect(x, 274, 256, 79, "#edf3e8")
            text(x + 16, 303, title, 20, green, "700")
            text(x + 16, 330, detail, 16, muted)
            if i < 2:
                text(x + 263, 320, ">", 18, muted)
        rect(324, 372, 816, 192, "#fafbf8", "#dce4dc")
        text(344, 401, "OPERATION NOTES / SOURCE-LINKED HISTORY", 12, green, "700", True)
        text(344, 438, "Intent: keep redirects below 100 ms", 21, weight="600")
        text(344, 471, "Result: the first latency check failed", 20)
        text(344, 503, "Pointers back to the task and recorded check", 17, muted)
        line(344, 519, 1120, 519)
        text(344, 545, "RAW CAPTURE / NOT AN ADMITTED LESSON", 13, "#866322", "700", True)
        text(324, 597, "LLM extraction: off by default; a custom adapter is required.", 16, muted)
    elif key == "analytics":
        for x, title, value, detail in ((324, "RESPONSES", "48", "sample session"),
                                       (604, "LATEST INPUT", "64K", "observed context"),
                                       (884, "EST. API COST", "$0.84", "illustrative estimate")):
            rect(x, 270, 256, 96, "#f3f5f0")
            text(x + 16, 294, title, 12, muted, "700", True)
            text(x + 16, 326, value, 28, blue if x == 604 else ink, "600")
            text(x + 16, 349, detail, 13, muted)
        text(324, 401, "Context over time", 19, weight="600")
        text(1013, 401, "64K input", 15, muted)
        line(324, 490, 1140, 490)
        line(324, 420, 1140, 420)
        points = [(324, 484), (370, 472), (430, 468), (495, 468), (554, 451), (627, 451),
                  (691, 447), (752, 447), (818, 435), (879, 431), (948, 431), (1013, 422), (1076, 416), (1140, 411)]
        poly = " ".join(f"{x},{y}" for x, y in points)
        parts.append(f'<polygon points="324,490 {poly} 1140,490" fill="#edf0ff"/>')
        parts.append(f'<polyline points="{poly}" fill="none" stroke="{blue}" stroke-width="3"/>')
        for x, y in points:
            dot(x, y, blue, 4)
        text(324, 518, "Each point leads back to its captured conversation.", 15, muted)
        text(324, 557, "Tools used", 17, weight="600")
        for y, name, width in ((582, "Shell", 524), (608, "Read", 238)):
            text(324, y, name, 15, muted)
            rect(425, y - 11, 715, 9, "#e8ede5", radius=4)
            rect(425, y - 11, width, 9, blue, radius=4)
    else:
        raise ValueError(f"Unknown hub tour scene: {key}")

    rect(32, 661, 1136, 43, "#eaf0e4", radius=8)
    text(49, 688, scene["takeaway"], 18, "#456047")
    text(32, 726, "Simplified feature illustrations / invented sample data / not a live feed or exact UI reproduction", 12, muted)
    parts.append('</svg>')
    return "\n".join(parts) + "\n"


def hub_tour():
    story = json.loads((ASSETS / "hub-tour.json").read_text(encoding="utf-8"))
    frames = []
    for i, scene in enumerate(story["scenes"]):
        source = hub_tour_svg(story, scene)
        name = "hub-tour.svg" if i == 0 else f'hub-tour-{scene["id"]}.svg'
        (ASSETS / name).write_text(source, encoding="utf-8")
        frames.append(frame(source, {}))
    palette_source = Image.new("RGB", (300 * len(frames), 245))
    for i, picture in enumerate(frames):
        palette_source.paste(picture.resize((300, 185)), (i * 300, 0))
    # Pale panels occupy most pixels. Give small status marks enough palette weight
    # to retain their green, blue and amber instead of collapsing to gray.
    accents = ["#217451", "#4b58cc", "#866322", "#f1b2a5", "#e9c989",
               "#243e33", "#65766d", "#172b22", "#e8f3eb", "#edf0ff"]
    width = palette_source.width // len(accents)
    for i, color in enumerate(accents):
        palette_source.paste(color, (i * width, 185, (i + 1) * width, 245))
    save("hub-tour", frames, [s["duration"] for s in story["scenes"]], palette_source)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=("banner", "terminal", "workflow", "hub", "hub-tour"))
    selected = parser.parse_args().only
    for name, render in (("banner", banner), ("terminal", terminal), ("workflow", workflow), ("hub", hub), ("hub-tour", hub_tour)):
        if selected is None or selected == name:
            render()
