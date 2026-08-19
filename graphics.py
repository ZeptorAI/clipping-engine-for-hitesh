#!/usr/bin/env python3
"""
Phase 4 — detect on-screen graphics that the 9:16 center-crop would chop, so the
render can show the WHOLE frame during exactly those shots.

The default reframe keeps only the CENTER third of the source width; anything in
the left/right third is lost. This splits a clip into SHOTS (ffmpeg scene
detection), checks ONE frame per shot with vision, and returns fit-windows that
line up exactly with the graphic shots — no bleed onto the talking head.

windows_for_clip(video, ranges) -> (windows[[s,e]], usage)
detect(video, ranges)           -> {hits, cost_usd} (standalone testing)
"""

import base64
import json
import os
import re
import subprocess
import tempfile

import brain  # key handling / model defaults / rates

KEEP_LEFT, KEEP_RIGHT = 0.342, 0.658  # center strip that survives the crop

SYSTEM = (
    "You inspect frames from a 16:9 video being converted to a 9:16 vertical clip. "
    f"The conversion keeps ONLY the center vertical strip (width {int(KEEP_LEFT*100)}%"
    f"-{int(KEEP_RIGHT*100)}%). The left {int(KEEP_LEFT*100)}% and right "
    f"{100-int(KEEP_RIGHT*100)}% are cut off.\n\n"
    "Your ONLY job: report EDITED-IN graphic overlays (text titles, callouts, "
    "lower-thirds, product cards, charts, diagrams, on-screen labels, before/after "
    "images) that sit in the left or right cut zones and would be lost.\n\n"
    "CRITICAL RULES:\n"
    "- Report ONLY text/graphics you can LITERALLY SEE in THIS frame's pixels. "
    "Never guess or infer from the video's topic.\n"
    "- A person, wall, curtain, furniture, bed, door, plant, or blurry background "
    "is NOT a graphic. Ordinary room/scenery in the side zones is fine → cropped:false.\n"
    "- Most talking-head frames have NO editor graphics → cropped:false, empty items. "
    "That is the common, correct answer.\n"
    "- If unsure, return cropped:false.\n\n"
    "Return ONLY JSON: {\"frames\":[{\"t\":<seconds>,\"cropped\":true|false,"
    "\"items\":[{\"text\":\"exact words you can read\","
    "\"kind\":\"title|callout|product|chart|label|image|other\","
    "\"side\":\"left|right|full\"}]}]}"
)


# ---------------------------------------------------------------- frames / vision
def _grab(video, times):
    out, tmp = [], tempfile.mkdtemp(prefix="gfx_")
    for i, t in enumerate(times):
        p = os.path.join(tmp, f"f{i}.jpg")
        subprocess.run(["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", video,
                        "-frames:v", "1", "-vf", "scale=1024:-1", "-q:v", "4", p],
                       capture_output=True)
        if os.path.isfile(p):
            out.append((round(float(t), 2), p))
    return out


def _parse(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(m.group(0) if m else text)


def _analyze(frames, model=None):
    """One vision call over labeled frames -> (per_frame list, cost_usd, model)."""
    if not frames:
        return [], 0.0, None
    brain._load_env()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("No ANTHROPIC_API_KEY.")
    model = (model or os.environ.get("GRAPHICS_MODEL")
             or os.environ.get("CLIP_MODEL") or brain.DEFAULT_MODEL)

    import anthropic
    client = anthropic.Anthropic(api_key=key)
    content = [{"type": "text", "text": "Frames follow, each labeled with its timestamp."}]
    for t, p in frames:
        with open(p, "rb") as f:
            b = base64.b64encode(f.read()).decode()
        content.append({"type": "text", "text": f"Frame at t={t}s:"})
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": b}})
    resp = client.messages.create(model=model, max_tokens=4000, system=SYSTEM,
                                  messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    per = _parse(text).get("frames", [])
    u = resp.usage
    in_rate, out_rate = brain.RATES.get(model, (5.0, 25.0))
    cost = u.input_tokens / 1e6 * in_rate + u.output_tokens / 1e6 * out_rate
    return per, round(cost, 4), model


# ------------------------------------------------------------------ scene shots
def _scene_cuts(video, start, end, threshold=0.4):
    dur = end - start
    if dur <= 0.25:
        return []
    p = subprocess.run(
        ["ffmpeg", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", video,
         "-an", "-filter:v", f"select='gt(scene,{threshold})',showinfo",
         "-f", "null", "-"], capture_output=True, text=True)
    cuts = [start + float(m.group(1))
            for m in re.finditer(r"pts_time:([0-9.]+)", p.stderr)]
    return sorted(c for c in cuts if start < c < end)


def _shots(video, ranges, max_shots=12):
    shots = []
    for rs, re_ in ranges:
        rs, re_ = float(rs), float(re_)
        bounds = [rs] + _scene_cuts(video, rs, re_) + [re_]
        for a, b in zip(bounds, bounds[1:]):
            if b - a > 0.35:
                shots.append([round(a, 2), round(b, 2)])
    if len(shots) > max_shots:                       # cap frames on cut-heavy clips
        step = len(shots) / max_shots
        shots = [shots[int(i * step)] for i in range(max_shots)]
    return shots


def _merge(wins):
    wins = sorted([[float(a), float(b)] for a, b in wins if b > a])
    out = []
    for w in wins:
        if out and w[0] <= out[-1][1] + 0.2:
            out[-1][1] = max(out[-1][1], w[1])
        else:
            out.append(w)
    return out


def _clamp(wins, ranges):
    out = []
    for ws, we in wins:
        for rs, re_ in ranges:
            a, b = max(ws, float(rs)), min(we, float(re_))
            if b - a > 0.35:
                out.append([round(a, 2), round(b, 2)])
    return _merge(out)


# ----------------------------------------------------------------------- public
def windows_for_clip(video, ranges):
    """Scene-aware: one frame per shot; graphic shots become exact fit-windows."""
    shots = _shots(video, ranges)
    if not shots:
        return [], {"cost_usd": 0, "model": None}
    times = [round((a + b) / 2.0, 2) for a, b in shots]
    frames = _grab(video, times)
    per, cost, model = _analyze(frames)
    graphic_at = {round(float(f.get("t")), 2): bool(f.get("cropped")) and bool(f.get("items"))
                  for f in per}
    gshots = [[a, b] for (a, b), t in zip(shots, times) if graphic_at.get(round(t, 2))]
    wins = _clamp(_merge(gshots), ranges)
    return wins, {"cost_usd": cost, "model": model}


def detect(video, ranges, every=2.0, max_frames=8):
    """Time-sampled detection (kept for standalone testing / debugging)."""
    times = []
    for s, e in ranges:
        t = float(s)
        while t < float(e):
            times.append(round(t, 2)); t += every
    if len(times) > max_frames:
        step = len(times) / max_frames
        times = [times[int(i * step)] for i in range(max_frames)]
    frames = _grab(video, times)
    per, cost, model = _analyze(frames)
    hits = [f for f in per if f.get("cropped") and f.get("items")]
    return {"hits": hits, "cost_usd": cost, "model": model}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 4:
        sys.exit("usage: python graphics.py <video> <start> <end>")
    v, s, e = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    wins, usage = windows_for_clip(v, [[s, e]])
    print("fit windows:", wins, "| usage:", usage)
