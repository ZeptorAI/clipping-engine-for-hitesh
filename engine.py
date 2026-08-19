#!/usr/bin/env python3
"""
ZeptorAI Clipping Engine
========================
Two stages, so the *judgment* stays with Claude (per the Clipping Rulebook)
while transcription and cutting stay local and free.

  Stage 1 - transcribe:
      python engine.py transcribe "my-video.mp4"
      -> writes output/my-video.transcript.txt  (timestamped, Claude reads this)

  Stage 2 - render:
      python engine.py render "my-video__clips.json"
      -> cuts + stitches each clip, reframes to 9:16, writes output/*.mp4

Claude reads the transcript, applies the rulebook, and writes the clips JSON
spec (schema in README.md). This script never decides what is clip-worthy -
it only transcribes and cuts what it is told to.
"""

import argparse
import json
import os
import re
import subprocess
import sys


# ============================================================================
# Shared helpers
# ============================================================================

def _fmt(t: float) -> str:
    """Seconds -> mm:ss.s for human reading."""
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:04.1f}"


def _out_dir() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(d, exist_ok=True)
    return d


# ============================================================================
# Stage 1 - transcribe
# ============================================================================

def build_sentences(video_path: str, model_size: str):
    from faster_whisper import WhisperModel

    print(f"[transcribe] Loading Whisper ({model_size}) - first run downloads the model...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(video_path, vad_filter=True, word_timestamps=True)
    print(f"[transcribe] Language: {info.language} ({info.language_probability:.0%})")

    words = []
    for seg in segments:
        if seg.words:
            words.extend(seg.words)
        else:
            words.append(type("W", (), {"word": seg.text, "start": seg.start, "end": seg.end}))

    # Break into short lines so clips get tight, natural cut points. Split on
    # terminal punctuation, on a noticeable pause between words, or when a line
    # runs too long (Whisper often leaves monologues unpunctuated).
    GAP = 0.6       # seconds of silence that counts as a break
    MAX_DUR = 8.0   # force a break if a line gets this long

    sentences, buf, buf_start, prev_end = [], [], None, None
    for w in words:
        if buf and prev_end is not None and (w.start - prev_end) > GAP:
            text = "".join(buf).strip()
            if text:
                sentences.append((buf_start, prev_end, text))
            buf, buf_start = [], None
        if buf_start is None:
            buf_start = w.start
        buf.append(w.word)
        prev_end = w.end
        if re.search(r"[.!?][\"')\]]*\s*$", w.word) or (prev_end - buf_start) > MAX_DUR:
            text = "".join(buf).strip()
            if text:
                sentences.append((buf_start, prev_end, text))
            buf, buf_start = [], None
    if buf and words:
        text = "".join(buf).strip()
        if text:
            sentences.append((buf_start, words[-1].end, text))

    words_out = [{"s": round(w.start, 2), "e": round(w.end, 2), "w": w.word.strip()}
                 for w in words]
    return sentences, words_out


def cmd_transcribe(args):
    if not os.path.isfile(args.video):
        sys.exit(f"File not found: {args.video}")

    sentences, words = build_sentences(args.video, args.model)
    if not sentences:
        sys.exit("No speech found in the video.")

    stem = os.path.splitext(os.path.basename(args.video))[0]
    with open(os.path.join(_out_dir(), f"{stem}.words.json"), "w", encoding="utf-8") as f:
        json.dump(words, f)
    out_path = os.path.join(_out_dir(), f"{stem}.transcript.txt")

    total = sentences[-1][1]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# Transcript: {os.path.basename(args.video)}\n")
        f.write(f"# Duration of speech: {_fmt(total)}  ({total:.1f}s)\n")
        f.write("# Format:  [mm:ss.s -> mm:ss.s | START-END sec]  text\n")
        f.write("# Use the raw START-END seconds when writing clip ranges.\n\n")
        for start, end, text in sentences:
            f.write(f"[{_fmt(start)} -> {_fmt(end)} | {start:.1f}-{end:.1f}]  {text}\n")

    print(f"[transcribe] {len(sentences)} sentences, {total/60:.1f} min of speech.")
    print(f"[transcribe] Wrote: {out_path}")
    print("[transcribe] Next: Claude reads this and writes the clips JSON, then run 'render'.")


# ============================================================================
# Stage 2 - render (cut + stitch + reframe to 9:16)
# ============================================================================

REFRAME = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1"


def _even(n):
    n = int(round(n))
    return n if n % 2 == 0 else n - 1


def _video_filter(idx: int, layout) -> str:
    """Build the video filter for input `idx`, producing label [v{idx}].

    layout=None -> center-crop reframe (default).
    layout={"type":"split", ...} -> screenshare on top, webcam on bottom.
        screen : [x,y,w,h]  region of the shared screen (omit/None = full frame)
        cam    : [x,y,w,h]  region of the webcam PIP
        split  : fraction of height given to the top (screen) panel (default 0.4)
    """
    if not layout or layout.get("type") != "split":
        return f"[{idx}:v]{REFRAME}[v{idx}]"

    top_h = _even(1920 * float(layout.get("split", 0.4)))
    bot_h = 1920 - top_h

    # top panel: screenshare.
    #   fit="cover"  -> fill the panel, crop overflowing edges (no black bars)
    #   fit="contain"-> show the whole region, letterbox with black bars
    screen = layout.get("screen")
    crop_s = f"crop={_even(screen[2])}:{_even(screen[3])}:{int(screen[0])}:{int(screen[1])}," if screen else ""
    if layout.get("fit", "cover") == "contain":
        fit_s = (f"scale=1080:{top_h}:force_original_aspect_ratio=decrease,"
                 f"pad=1080:{top_h}:-1:-1:color=black")
    else:
        fit_s = (f"scale=1080:{top_h}:force_original_aspect_ratio=increase,"
                 f"crop=1080:{top_h}")
    top = f"[s{idx}]{crop_s}{fit_s},setsar=1[top{idx}]"

    # bottom panel: webcam, scaled to fill (cover + crop)
    cx, cy, cw, ch = layout["cam"]
    bottom = (f"[c{idx}]crop={_even(cw)}:{_even(ch)}:{int(cx)}:{int(cy)},"
              f"scale=1080:{bot_h}:force_original_aspect_ratio=increase,"
              f"crop=1080:{bot_h},setsar=1[bot{idx}]")

    stack = f"[top{idx}][bot{idx}]vstack=inputs=2[v{idx}]"
    return f"[{idx}:v]split=2[s{idx}][c{idx}];{top};{bottom};{stack}"


def render_clip(video_path: str, ranges, out_path: str, layout=None, pad: float = 0.15):
    """Cut each [start,end] range, reframe/lay out, concat into one clip."""
    if not ranges:
        raise ValueError("clip has no ranges")

    cmd = ["ffmpeg", "-y"]
    for start, end in ranges:
        s = max(0.0, float(start) - pad)
        d = (float(end) + pad) - s
        cmd += ["-ss", f"{s:.3f}", "-t", f"{d:.3f}", "-i", video_path]

    parts, vlabels, alabels = [], [], []
    for idx in range(len(ranges)):
        parts.append(_video_filter(idx, layout))
        parts.append(f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo[a{idx}]")
        vlabels.append(f"[v{idx}]")
        alabels.append(f"[a{idx}]")

    n = len(ranges)
    concat = "".join(v + a for v, a in zip(vlabels, alabels))
    concat += f"concat=n={n}:v=1:a=1[v][a]"
    filter_complex = ";".join(parts) + ";" + concat

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        out_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr[-1500:], file=sys.stderr)
        raise RuntimeError(f"ffmpeg failed for {out_path}")


def _fit_filter(idx: int) -> str:
    """Show the WHOLE 16:9 frame inside 9:16 on a blurred fill (nothing cropped).
    Used during on-screen graphics that a center-crop would destroy."""
    return (f"[{idx}:v]split=2[bg{idx}][fg{idx}];"
            f"[bg{idx}]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,gblur=sigma=28[bb{idx}];"
            f"[fg{idx}]scale=1080:1920:force_original_aspect_ratio=decrease[ff{idx}];"
            f"[bb{idx}][ff{idx}]overlay=(W-w)/2:(H-h)/2,setsar=1[v{idx}]")


def render_framed(video_path: str, segments, out_path: str):
    """Render a clip from time segments, each framed 'crop' (talking head) or
    'fit' (whole frame, for graphics). segments = [{start,end,mode}] in source secs."""
    segments = [s for s in segments if float(s["end"]) - float(s["start"]) > 0.03]
    if not segments:
        raise ValueError("no segments")

    cmd = ["ffmpeg", "-y"]
    for seg in segments:
        s = max(0.0, float(seg["start"]))
        d = float(seg["end"]) - s
        cmd += ["-ss", f"{s:.3f}", "-t", f"{d:.3f}", "-i", video_path]

    parts, vlabels, alabels = [], [], []
    for idx, seg in enumerate(segments):
        parts.append(_fit_filter(idx) if seg.get("mode") == "fit"
                     else f"[{idx}:v]{REFRAME}[v{idx}]")
        parts.append(f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo[a{idx}]")
        vlabels.append(f"[v{idx}]")
        alabels.append(f"[a{idx}]")

    n = len(segments)
    concat = "".join(v + a for v, a in zip(vlabels, alabels))
    concat += f"concat=n={n}:v=1:a=1[v][a]"
    filter_complex = ";".join(parts) + ";" + concat
    cmd += ["-filter_complex", filter_complex, "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr[-1500:], file=sys.stderr)
        raise RuntimeError(f"ffmpeg failed for {out_path}")


def cmd_render(args):
    if not os.path.isfile(args.spec):
        sys.exit(f"Spec not found: {args.spec}")

    with open(args.spec, encoding="utf-8-sig") as f:  # tolerate a BOM
        spec = json.load(f)

    video = spec["source"]
    if not os.path.isabs(video):
        # resolve relative to the spec file's location, then the engine folder
        base = os.path.dirname(os.path.abspath(args.spec))
        cand = os.path.join(base, video)
        video = cand if os.path.isfile(cand) else video
    if not os.path.isfile(video):
        sys.exit(f"Source video not found: {video}")

    stem = os.path.splitext(os.path.basename(video))[0]
    out_dir = _out_dir()
    clips = spec.get("clips", [])
    if not clips:
        sys.exit("No clips in spec.")

    print(f"[render] {len(clips)} clip(s) from {os.path.basename(video)}")
    for i, clip in enumerate(clips, 1):
        cid = clip.get("id", f"clip{i}")
        ranges = clip["ranges"]
        out_mp4 = os.path.join(out_dir, f"{stem}__{cid}.mp4")
        dur = sum((float(e) - float(s)) for s, e in ranges)
        layout = clip.get("layout")
        tag = f" [{layout['type']}]" if layout else ""
        print(f"  [{i}/{len(clips)}] {cid}: {len(ranges)} range(s), ~{dur:.0f}s{tag} -> {os.path.basename(out_mp4)}")
        render_clip(video, ranges, out_mp4, layout=layout, pad=float(clip.get("pad", 0.15)))

        # sidecar notes so each clip carries its rulebook rationale
        with open(os.path.join(out_dir, f"{stem}__{cid}.txt"), "w", encoding="utf-8") as f:
            f.write(f"ICP:     {clip.get('icp','')}\n")
            f.write(f"Hook:    {clip.get('hook','')}\n")
            f.write(f"Promise: {clip.get('promise','')}\n")
            f.write(f"Why:     {clip.get('why','')}\n")
            f.write(f"Ranges:  {ranges}\n")

    print(f"[render] Done. Clips in: {out_dir}")


# ============================================================================
# main
# ============================================================================

def main():
    ap = argparse.ArgumentParser(description="ZeptorAI Clipping Engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("transcribe", help="transcribe a long-form video")
    t.add_argument("video")
    t.add_argument("--model", default="small",
                   help="whisper model: tiny/base/small/medium (bigger = better, slower)")
    t.set_defaults(func=cmd_transcribe)

    r = sub.add_parser("render", help="cut + stitch + reframe clips from a JSON spec")
    r.add_argument("spec", help="path to the clips JSON spec")
    r.set_defaults(func=cmd_render)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
