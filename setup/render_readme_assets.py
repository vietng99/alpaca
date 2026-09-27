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
    # A neutral matte also covers any transparent pixels in source artwork.
    background = Image.new("RGB", rgba.size, "#0d1117")
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
            "eyes": {"transform": f"translate(0 93) scale(1 {blink}) translate(0 -93)"},
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
    """Six visible steps, one artifact and one next action per frame."""
    colors = {"text": "#e6edf3", "muted": "#9da7b3", "accent": "#79b8ff",
              "good": "#79b8ff", "warn": "#ffb48a"}
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="600" viewBox="0 0 1200 600" role="img" aria-labelledby="title desc">',
             '<title id="title">Idea to proof</title>',
             '<desc id="desc">Example workflow: spec, runbook, checklist, agent work, a failed check, a permitted retry and a sealed report. Release needs owner approval.</desc>',
             '<rect width="1200" height="600" fill="#0d1117"/>']

    def rect(x, y, w, h, fill, stroke=None):
        extra = f' stroke="{stroke}"' if stroke else ''
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}"{extra}/>')

    def text(x, y, value, size=22, color="#e6edf3", weight="400", mono=False):
        family = "DejaVu Sans Mono, monospace" if mono else "Arial, Helvetica, sans-serif"
        space = ' xml:space="preserve"' if mono else ''
        parts.append(f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="{weight}" fill="{color}"{space}>{escape(value)}</text>')

    text(40, 67, story["title"], 36, weight="700")
    text(1160, 65, "EXAMPLE / LINK SHORTENER", 13, "#9da7b3", mono=True)
    # Right-align the example label, without depending on font width.
    parts[-1] = parts[-1].replace('x="1160"', 'x="1160" text-anchor="end"')
    step = scene["step"]
    for index, label in enumerate(story["steps"]):
        x = 40 + index * 190
        active = index == step
        rect(x, 102, 170, 52, "#245da8" if active else "#161b22")
        color = "#ffffff" if active else "#b7c3d0" if index < step else "#9da7b3"
        text(x + 13, 134, f"{index + 1:02}", 13, color, "700", True)
        text(x + 41, 134, label, 17, color, "700" if active else "400")
        if index < 5:
            text(x + 177, 133, ">", 15, "#6e7681")

    rect(40, 186, 730, 332, "#161b22", "#30363d")
    text(64, 223, scene["file"], 15, "#9da7b3", mono=True)
    parts.append('<path d="M40 243H770" stroke="#30363d"/>')
    text(64, 286, scene["heading"], 28, weight="700")
    for index, (kind, line) in enumerate(scene["lines"]):
        text(64, 333 + index * 34, line, 20, colors[kind], mono=True)

    rect(790, 186, 370, 332, "#161b22", "#30363d")
    text(816, 223, "NEXT ACTION", 13, "#9da7b3", "700", True)
    for index, line in enumerate(textwrap.wrap(scene["action"], 22)):
        text(816, 274 + index * 32, line, 28, weight="700")
    for index, line in enumerate(textwrap.wrap(scene["body"], 27)):
        text(816, 355 + index * 28, line, 21, "#b7c3d0")
    color = "#ffb48a" if scene["status"] in ("FAIL", "RETRY") else "#79b8ff"
    text(816, 488, scene["status"], 15, color, "700", True)
    text(40, 563, scene["footer"], 18, "#9da7b3")
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
        "pass": ("#203651", "#a5c9ff", "PASS", "PROOF SEALED"),
        "live": ("#1b2b44", "#79b8ff", "LIVE", "RUNNING NOW"),
        "waiting": ("#161b22", "#6e7681", "NO RUN", "NOT STARTED"),
        "fail": ("#322027", "#ff938a", "FAIL", "FAILED ATTEMPT"),
        "paused": ("#30291c", "#e6b768", "PAUSED", "OWNER CHECK-IN"),
    }
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="620" viewBox="0 0 1200 620" role="img" aria-labelledby="title desc">',
             '<title id="title">Watch the work move through the hub.</title>',
             '<desc id="desc">Simplified illustrative hub: five connected stages and their task cards. Bright blue is live, pale blue is passing work, red is a failed attempt and amber is an owner check-in. Release runs only after an explicit owner approval.</desc>',
             '<rect width="1200" height="620" fill="#0d1117"/>']

    def text(x, y, value, size=16, color="#e6edf3", weight="400", mono=False):
        family = "DejaVu Sans Mono, monospace" if mono else "Arial, Helvetica, sans-serif"
        parts.append(f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="{weight}" fill="{color}">{escape(value)}</text>')

    def card(x, y, title, state, task=False):
        fill, color, stage_label, task_label = states[state]
        dashed = ' stroke-dasharray="5 4"' if state == "waiting" else ''
        parts.append(f'<rect x="{x}" y="{y}" width="208" height="92" rx="8" fill="{fill}" stroke="{color}" stroke-width="1.5"{dashed}/>')
        parts.append(f'<rect x="{x}" y="{y + 1}" width="4" height="90" rx="2" fill="{color}"/>')
        text(x + 14, y + 31, title, 18, weight="600")
        label_color = "#9da7b3" if state == "waiting" else color
        text(x + 14, y + 72, task_label if task else stage_label, 12, label_color, mono=True)
        if state == "live":
            parts.append(f'<circle cx="{x + 188}" cy="{y + 24}" r="4" fill="{color}"/>')

    text(40, 39, "ALPACA / OPERATIONS HUB", 12, "#79b8ff", "700", True)
    text(40, 84, "Watch the work move.", 36, weight="700")
    text(40, 115, "Stages, tasks and proof.", 18, "#9da7b3")
    done = scene["states"].count("pass")
    text(943, 46, f"{done} / 5 stages passed", 16, "#a5c9ff", "600")
    text(943, 74, scene["owner"], 13, "#e6b768")
    for x, width, label in ((40, 436, "PLAN"), (496, 436, "EXECUTE"), (952, 208, "OWNER GATE")):
        text(x + 7, 151, label, 11, "#9da7b3", "700", True)
        parts.append(f'<path d="M{x} 169v-9h{width}v9" fill="none" stroke="#30363d" stroke-width="1.5"/>')
    for i, (stage, task, state) in enumerate(zip(story["stages"], story["tasks"], scene["states"])):
        x = 40 + i * 228
        card(x, 180, stage, state)
        if i < 4:
            parts.append(f'<path d="M{x + 209} 226h15m-5-4 5 4-5 4" fill="none" stroke="#6e7681" stroke-width="1.5"/>')
        parts.append(f'<path d="M{x + 104} 281v41m-4-5 4 5 4-5" fill="none" stroke="#6e7681" stroke-width="1.3" stroke-dasharray="3 4"/>')
        card(x, 333, task, state, task=True)
    parts.append('<rect x="40" y="447" width="1120" height="82" rx="8" fill="#161b22"/>')
    text(58, 478, scene["event"], 17, "#e6edf3", "600")
    text(58, 507, scene["history"], 15, "#9da7b3")
    for x, color, label in ((40, "#a5c9ff", "PASS / SEALED"), (266, "#79b8ff", "LIVE"), (415, "#ff938a", "FAILED"), (590, "#e6b768", "CHECK-IN"), (791, "#6e7681", "NOT STARTED")):
        parts.append(f'<circle cx="{x + 5}" cy="564" r="5" fill="{color}"/>')
        text(x + 19, 568, label, 12, "#9da7b3", mono=True)
    text(40, 603, "Example data. Stages come from your project profile.", 12, "#9da7b3")
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
    ink, muted, complete, blue = "#e6edf3", "#9da7b3", "#a5c9ff", "#79b8ff"
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="740" viewBox="0 0 1200 740" role="img" aria-labelledby="title desc">',
             f'<title id="title">Alpaca hub tour: {escape(scene["label"])}</title>',
             f'<desc id="desc">Simplified feature illustration with invented sample data. {escape(scene["subtitle"])} {escape(scene["takeaway"])}</desc>']

    def rect(x, y, w, h, fill="#161b22", stroke=None, radius=10):
        extra = f' stroke="{stroke}"' if stroke else ''
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}"{extra}/>')

    def text(x, y, value, size=18, color=ink, weight="400", mono=False):
        family = "DejaVu Sans Mono, monospace" if mono else "Arial, Helvetica, sans-serif"
        parts.append(f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="{weight}" fill="{color}">{escape(str(value))}</text>')

    def line(x1, y1, x2, y2, color="#30363d"):
        parts.append(f'<path d="M{x1} {y1}L{x2} {y2}" stroke="{color}" fill="none"/>')

    def dot(x, y, color, radius=5):
        parts.append(f'<circle cx="{x}" cy="{y}" r="{radius}" fill="{color}"/>')

    rect(0, 0, 1200, 740, "#0d1117", radius=0)
    text(32, 39, "ALPACA / PROJECT HUB", 12, blue, "700", True)
    text(32, 88, story["title"], 36, weight="700")
    text(32, 120, "Tasks, agents, logs and proof.", 19, muted)
    line(32, 143, 1168, 143)
    text(32, 181, "FEATURE TOUR", 12, muted, "700", True)
    nav_pitch = min(78, 420 / len(story["scenes"]))
    for i, item in enumerate(story["scenes"]):
        y = 196 + i * nav_pitch
        active = item["id"] == scene["id"]
        if active:
            rect(32, y, 240, min(66, nav_pitch - 9), "#1b2b44")
            rect(32, y + 8, 4, min(46, nav_pitch - 25), blue, radius=2)
        text(49, y + 24, f"{i + 1:02}", 13, blue if active else muted, "700", True)
        text(82, y + 24, item["label"], 17, weight="700" if active else "400")
        text(82, y + 44, item["hint"], 13, muted)
    text(48, 624, "ONE SHARED RECORD", 12, muted, "700", True)

    rect(296, 169, 872, 469, stroke="#30363d", radius=14)
    text(324, 211, scene["heading"], 28, weight="700")
    text(324, 242, scene["subtitle"], 18, muted)
    key = scene["id"]
    if key == "sessions":
        dot(330, 282, complete)
        text(345, 288, "2 active project sessions", 17, complete, "600")
        text(900, 288, "1 idle in recent window", 16, muted)
        cards = [(324, "Implementer", "3s ago / Edit", "03", "Build redirect handler", "Keep redirects below 100 ms.", "Implement the agreed contract."),
                 (742, "Reviewer", "9s ago / Shell", "04", "Check edge cases", "Check timeout and retry cases.", "Record the failures and proof.")]
        for x, title, beat, number, task, ask1, ask2 in cards:
            rect(x, 310, 398, 242, "#0d1117", "#30363d")
            dot(x + 20, 336, complete)
            text(x + 35, 343, title, 22, weight="600")
            text(x + 18, 372, "Heartbeat " + beat, 15, muted)
            line(x + 18, 386, x + 380, 386)
            text(x + 18, 412, "TASK " + number + " / DOING", 12, blue, "700", True)
            text(x + 18, 441, task, 21, weight="600")
            text(x + 18, 477, "LATEST CAPTURED ASK", 11, muted, "700", True)
            text(x + 18, 502, ask1, 17)
            text(x + 18, 527, ask2, 17)
        text(324, 584, "Open a session to read its conversation.", 17, muted)
        text(324, 614, "Agent crew: captured parent and child sessions.", 16, complete)
    elif key == "work":
        text(324, 285, "3 sealed", 16, complete, "700")
        text(462, 285, "1 running", 16, blue, "700")
        text(613, 285, "1 open", 16, muted)
        text(1030, 285, "5 tasks", 16, muted)
        rect(324, 301, 816, 10, "#30363d", radius=5)
        rect(324, 301, 490, 10, complete, radius=5)
        rect(819, 301, 158, 10, blue, radius=0)
        rows = [("03", "Cover edge cases", "Done", "Result recorded / proof linked", complete, "#203651"),
                ("04", "Check redirect latency", "Doing", "Current attempt / method recorded", blue, "#1b2b44"),
                ("05", "Release + health check", "Open", "Owner gate / not started", muted, "#21262d")]
        for i, (number, title, status, detail, color, fill) in enumerate(rows):
            y = 330 + i * 82
            rect(324, y, 816, 71, fill)
            rect(324, y + 8, 4, 55, color, radius=2)
            text(343, y + 31, number, 15, color, mono=True)
            text(384, y + 29, title, 21, weight="600")
            text(384, y + 53, detail, 15, muted)
            text(1047, y + 30, status, 17, color, "700")
        text(324, 605, "Open the report  >  inspect evidence  >  reproduce the result", 17, complete)
    elif key == "runs":
        for x, label in ((324, "COMPONENT"), (518, "LINT"), (735, "TEST"), (952, "BUILD")):
            text(x, 284, label, 12, muted, "700", True)
        for i, (name, states) in enumerate((("API", ["PASS"] * 3), ("Worker", ["PASS", "LIVE", "NO RUN"]), ("Web", ["PASS"] * 3))):
            y = 302 + i * 63
            text(324, y + 31, name, 19, weight="600")
            for j, state in enumerate(states):
                x = 500 + j * 217
                color, fill = (complete, "#203651") if state == "PASS" else (blue, "#1b2b44") if state == "LIVE" else (muted, "#21262d")
                rect(x, y, 198, 48, fill, color if state != "NO RUN" else "#30363d", 7)
                text(x + 18, y + 30, state, 17, color, "600")
        rect(324, 510, 816, 100, "#1b2b44")
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
            rect(x, 310, 398, 250, "#0d1117", "#30363d")
            text(x + 18, 344, title, 22, weight="600")
            text(x + 18, 371, "Pause / Focus / Open full run", 14, blue)
            rect(x + 10, 389, 378, 116, "#0d1117", radius=6)
            for i, value in enumerate((a, b, c)):
                text(x + 24, 420 + i * 29, value, 16, "#c9d1d9", mono=True)
            text(x + 18, 531, counts, 15, muted)
            text(x + 262, 531, memory, 14, muted)
        text(324, 594, "Follow stage changes. Pin finished jobs to compare.", 17, muted)
        text(324, 619, "Jobs and memory readings depend on your profile.", 15, muted)
    elif key == "logs":
        rect(324, 271, 816, 153, "#0d1117")
        log = [("042  INFO   Running latency checks", "#c9d1d9"),
               ("043  WARN   Slow response detected", "#e6b768"),
               ("044  ERROR  Expected p95 < 100 ms", "#ff938a"),
               ("045  ERROR  Observed p95 = 127 ms", "#ff938a")]
        for i, (value, color) in enumerate(log):
            text(344, 303 + i * 30, value, 19, color, mono=True)
        text(324, 452, "PROFILE-PROVIDED AGENT REVIEW", 12, muted, "700", True)
        rect(324, 467, 816, 143, "#30291c")
        text(344, 496, "The quoted result exceeds the latency limit.", 21, weight="600")
        text(344, 526, "Lines 044-045 / quotes matched to the recorded log", 17, muted)
        text(344, 555, "Check the slow path before the next attempt.", 18)
        rect(344, 570, 178, 26, "#45381f", radius=5)
        text(354, 588, "Not checked by engineer", 13, "#e6b768")
    elif key == "wiki":
        for i, (title, detail) in enumerate((("Capture", "Session events"), ("Keep", "Project-local vault"), ("Trace", "Source pointers"))):
            x = 324 + i * 280
            rect(x, 274, 256, 79, "#21262d")
            text(x + 16, 303, title, 20, complete, "700")
            text(x + 16, 330, detail, 16, muted)
            if i < 2:
                text(x + 263, 320, ">", 18, muted)
        rect(324, 372, 816, 192, "#161b22", "#30363d")
        text(344, 401, "OPERATION NOTES / SOURCE-LINKED HISTORY", 12, complete, "700", True)
        text(344, 438, "Intent: keep redirects below 100 ms", 21, weight="600")
        text(344, 471, "Result: the first latency check failed", 20)
        text(344, 503, "Pointers back to the task and recorded check", 17, muted)
        line(344, 519, 1120, 519)
        text(344, 545, "RAW CAPTURE / NOT AN ADMITTED LESSON", 13, "#e6b768", "700", True)
        text(324, 597, "LLM extraction: off by default; a custom adapter is required.", 16, muted)
    elif key == "analytics":
        for x, title, value, detail in ((324, "RESPONSES", "48", "sample session"),
                                       (604, "LATEST INPUT", "64K", "observed context"),
                                       (884, "EST. API COST", "$0.84", "illustrative estimate")):
            rect(x, 270, 256, 96, "#21262d")
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
        parts.append(f'<polygon points="324,490 {poly} 1140,490" fill="#1b2b44"/>')
        parts.append(f'<polyline points="{poly}" fill="none" stroke="{blue}" stroke-width="3"/>')
        for x, y in points:
            dot(x, y, blue, 4)
        text(324, 518, "Each point leads back to its captured conversation.", 15, muted)
        text(324, 557, "Tools used", 17, weight="600")
        for y, name, width in ((582, "Shell", 524), (608, "Read", 238)):
            text(324, y, name, 15, muted)
            rect(425, y - 11, 715, 9, "#30363d", radius=4)
            rect(425, y - 11, width, 9, blue, radius=4)
    else:
        raise ValueError(f"Unknown hub tour scene: {key}")

    rect(32, 661, 1136, 43, "#161b22", radius=8)
    text(49, 688, scene["takeaway"], 18, "#b7c3d0")
    text(32, 726, "Example data. Simplified views.", 12, muted)
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
    # Panels occupy most pixels. Give small status marks enough palette weight
    # to retain their pale blue, bright blue and amber instead of collapsing to gray.
    accents = ["#a5c9ff", "#79b8ff", "#e6b768", "#ff938a", "#e6b768",
               "#e6edf3", "#9da7b3", "#0d1117", "#203651", "#1b2b44"]
    width = palette_source.width // len(accents)
    for i, color in enumerate(accents):
        palette_source.paste(color, (i * width, 185, (i + 1) * width, 245))
    save("hub-tour", frames, [s["duration"] for s in story["scenes"]], palette_source)


def diagrams():
    """Render the existing diagram models with the README's blue/neutral palette."""
    for name, model_name in (("architecture", "alpaca.architecture"), ("task-fsm", "alpaca-task.lifecycle")):
        model = json.loads((ASSETS / "diagrams" / (model_name + ".json")).read_text())
        for scheme in ("light", "dark"):
            dark = scheme == "dark"
            bg, surface = ("#0d1117", "#161b22") if dark else ("#ffffff", "#f3f5f8")
            ink, muted = ("#e6edf3", "#9da7b3") if dark else ("#253041", "#586579")
            blue, tint = ("#79b8ff", "#1b2b44") if dark else ("#386cbe", "#eaf1fc")
            border = "#53667f" if dark else "#b1c1d7"
            height = 540 if name == "architecture" else 470
            parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="{height}" viewBox="0 0 1200 {height}" text-rendering="geometricPrecision" role="img" aria-labelledby="title">',
                     f'<title id="title">{escape(model["meta"]["title"])}</title>',
                     f'<rect width="1200" height="{height}" fill="{bg}"/>',
                     f'<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0L8 4L0 8Z" fill="{blue}"/></marker></defs>']

            def text(x, y, value, size=14, color=ink, anchor="middle", bold=False):
                parts.append(f'<text x="{x}" y="{y}" font-family="Arial, Helvetica, sans-serif" font-size="{size}" font-weight="{700 if bold else 400}" text-anchor="{anchor}" fill="{color}">{escape(value)}</text>')

            text(40, 35, model["meta"]["title"], 24, anchor="start", bold=True)
            if name == "architecture":
                nodes = {n["id"]: dict(n, x=n["pos"][0], y=n["pos"][1], w=n["size"][0], h=n["size"][1]) for n in model["components"]}
                edges = model["connections"]
                parts.append(f'<rect x="967" y="70" width="199" height="394" rx="12" fill="{tint}" stroke="{border}" stroke-dasharray="5 5"/>')
                text(1066, 94, "Rebuilt from the record", 13, muted)
            else:
                nodes = {}
                for n in model["states"]:
                    x, y = (40 + n["col"] * 220, 170) if n["lane"] == "main" else (420, 340) if n["id"] == "blocked" else (920, 340)
                    nodes[n["id"]] = dict(n, x=x, y=y, w=180, h=76)
                edges = model["transitions"]

            def port(node, side):
                x, y, w, h = (node[k] for k in ("x", "y", "w", "h"))
                return {"left": (x, y+h/2), "right": (x+w, y+h/2), "top": (x+w/2, y), "bottom": (x+w/2, y+h)}[side]

            for edge in edges:
                a, b = nodes[edge["from"]], nodes[edge["to"]]
                side_a, side_b = edge.get("fromSide", "right"), edge.get("toSide", "left")
                x1, y1 = port(a, side_a)
                x2, y2 = port(b, side_b)
                if edge.get("route") == "top-channel":
                    channel = 82 if edge["id"] == "t-lease" else 119
                    path = f'M{x1} {y1}V{channel}H{x2}V{y2}'
                    lx, ly = (x1+x2)/2, channel-10
                elif side_a in ("top", "bottom"):
                    mid = (y1+y2)/2
                    path = f'M{x1} {y1}V{mid}H{x2}V{y2}'
                    lx, ly = (x1+x2)/2+27, mid-9
                elif side_a == "left" and side_b == "bottom":
                    path = f'M{x1} {y1}H{x2}V{y2}'
                    lx, ly = x2, y1+23
                else:
                    mid = (x1+x2)/2
                    path = f'M{x1} {y1}H{mid}V{y2}H{x2}'
                    lx, ly = mid, (y1+y2)/2-12
                dashed = ' stroke-dasharray="5 5"' if edge.get("variant") in ("dashed", "security") else ''
                parts.append(f'<path d="{path}" fill="none" stroke="{blue}" stroke-width="1.8"{dashed} marker-end="url(#arrow)"/>')
                if edge.get("label"):
                    label = edge["label"]
                    width = len(label)*7+12
                    parts.append(f'<rect x="{lx-width/2}" y="{ly-13}" width="{width}" height="18" fill="{bg}"/>')
                    text(lx, ly, label, 12, muted)
            for node in nodes.values():
                x, y, w, h = (node[k] for k in ("x", "y", "w", "h"))
                primary = node["id"] in ("cli", "record", "done", "gate")
                parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="{tint if primary else surface}" stroke="{blue if primary else border}" stroke-width="1.5"/>')
                text(x+w/2, y+h/2-3, node["label"], 16, bold=True)
                text(x+w/2, y+h/2+19, node["sublabel"], 12, muted)
            parts.append('</svg>')
            source = "\n".join(parts) + "\n"
            stem = f'{name}-{scheme}'
            (ASSETS / (stem + ".svg")).write_text(source)
            cairosvg.svg2png(bytestring=source.encode(), write_to=str(ASSETS / (stem + ".png")), scale=2)
            print(f'{stem}.png: {len(nodes)} nodes, {len(edges)} connections')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=("banner", "terminal", "workflow", "hub", "hub-tour", "diagrams"))
    selected = parser.parse_args().only
    for name, render in (("banner", banner), ("terminal", terminal), ("workflow", workflow), ("hub", hub), ("hub-tour", hub_tour), ("diagrams", diagrams)):
        if selected is None or selected == name:
            render()
