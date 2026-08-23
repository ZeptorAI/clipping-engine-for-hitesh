#!/usr/bin/env python3
"""
Per-job helpers used by Claude (the backend worker).

  python job_tools.py prep   <job_dir>
      -> reads the job's transcript.json, writes:
           <job_dir>/readable.txt   timestamped lines (Claude reads this)
           <job_dir>/words.tsv      start<TAB>end<TAB>token
      Prints only ASCII stats (Windows console can't print Devanagari).

  python job_tools.py render <job_dir>
      -> reads <job_dir>/clips.json (authored by Claude), finds the video,
         renders each clip to <job_dir>/<stem>__<id>.mp4, and updates
         status.json to done (or error) so the web page picks it up.

clips.json schema (same as engine.py's render spec, minus 'source'):
  { "clips": [ {"id","hook","why","pad","ranges":[[s,e],...],"layout"?}, ... ] }
"""

import json
import os
import re
import subprocess
import sys
import traceback

ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ENGINE_DIR)
import engine  # noqa: E402
import romanize  # noqa: E402  (Hinglish captions; same folder)
import brain  # noqa: E402  (slug helper for safe filenames)

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")


def _find_video(job_dir):
    for n in os.listdir(job_dir):
        if os.path.splitext(n)[1].lower() in VIDEO_EXTS:
            return os.path.join(job_dir, n)
    return None


def _flatten_words(transcript):
    """Return [(start, end, token)] from many possible transcript shapes."""
    def g(d, *names):
        for n in names:
            if n in d and d[n] is not None:
                return d[n]
        return None

    words = []
    segs = transcript.get("segments") if isinstance(transcript, dict) else None
    if segs:
        for seg in segs:
            ws = seg.get("words")
            if ws:
                for w in ws:
                    s = g(w, "start_time", "start", "s")
                    e = g(w, "end_time", "end", "e")
                    t = g(w, "text", "word", "w") or ""
                    if s is not None:
                        words.append((float(s), float(e if e is not None else s), t))
            else:  # segment-level only
                s = g(seg, "start_time", "start", "s")
                e = g(seg, "end_time", "end", "e")
                t = g(seg, "text", "word", "w") or ""
                if s is not None:
                    words.append((float(s), float(e if e is not None else s), t))
    elif isinstance(transcript, list):  # already a word list
        for w in transcript:
            s = g(w, "start_time", "start", "s")
            e = g(w, "end_time", "end", "e")
            t = g(w, "text", "word", "w") or ""
            if s is not None:
                words.append((float(s), float(e if e is not None else s), t))
    return words


def cmd_prep(job_dir):
    tr_path = os.path.join(job_dir, "transcript.json")
    with open(tr_path, encoding="utf-8-sig") as f:
        transcript = json.load(f)
    words = _flatten_words(transcript)
    if not words:
        sys.exit("prep: no words found in transcript.json")

    # words.tsv
    with open(os.path.join(job_dir, "words.tsv"), "w", encoding="utf-8") as f:
        for s, e, t in words:
            f.write(f"{s:.2f}\t{e:.2f}\t{t}\n")

    # readable.txt: group into lines on a >0.6s gap or >8s runtime
    GAP, MAX = 0.6, 8.0
    lines, buf, start, prev = [], [], None, None
    for s, e, t in words:
        if buf and prev is not None and (s - prev) > GAP:
            lines.append((start, prev, "".join(buf).strip())); buf, start = [], None
        if start is None:
            start = s
        buf.append(t if t.startswith(" ") else " " + t)
        prev = e
        if (prev - start) > MAX:
            lines.append((start, prev, "".join(buf).strip())); buf, start = [], None
    if buf:
        lines.append((start, prev, "".join(buf).strip()))

    with open(os.path.join(job_dir, "readable.txt"), "w", encoding="utf-8") as f:
        for a, b, txt in lines:
            f.write(f"[{a:.1f}-{b:.1f}]  {txt}\n")

    print(f"prep ok: {len(words)} words, {len(lines)} lines, "
          f"span {words[0][0]:.1f}-{words[-1][1]:.1f}s")


def build_segments(ranges, windows, pad):
    """Split a clip's ranges at graphic-window edges; each segment is
    'crop' (talking head) or 'fit' (whole frame, for a graphic). Preserves
    the same total timing/pad as a normal render so captions still line up."""
    segs = []
    for rs, re in ranges:
        rs, re = float(rs), float(re)
        cuts = {rs, re}
        for ws, we in windows:
            if we > rs and ws < re:
                cuts.add(max(rs, ws))
                cuts.add(min(re, we))
        cuts = sorted(cuts)
        rsegs = []
        for a, b in zip(cuts, cuts[1:]):
            if b - a < 0.05:
                continue
            mid = (a + b) / 2.0
            mode = "fit" if any(ws <= mid <= we for ws, we in windows) else "crop"
            rsegs.append({"start": a, "end": b, "mode": mode})
        if rsegs:
            rsegs[0]["start"] = max(0.0, rsegs[0]["start"] - pad)
            rsegs[-1]["end"] = rsegs[-1]["end"] + pad
        segs.extend(rsegs)
    return segs


def cmd_render(job_dir):
    status_path = os.path.join(job_dir, "status.json")

    def set_status(**kw):
        st = {}
        if os.path.isfile(status_path):
            with open(status_path, encoding="utf-8") as f:
                st = json.load(f)
        st.update(kw)
        with open(status_path, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)

    only = sys.argv[3] if len(sys.argv) > 3 else None  # render just one clip
    try:
        if not only:
            set_status(status="processing", message="Cutting the clips…")
        with open(os.path.join(job_dir, "clips.json"), encoding="utf-8-sig") as f:
            spec = json.load(f)
        clips = spec.get("clips", [])
        if not clips:
            raise ValueError("clips.json has no clips")

        video = _find_video(job_dir)
        if not video:
            raise ValueError("no video file in job folder")
        stem = os.path.splitext(os.path.basename(video))[0]
        job_id = os.path.basename(job_dir.rstrip("/\\"))

        out = []
        for i, clip in enumerate(clips, 1):
            cid = clip.get("id", f"clip{i}")
            if only and cid != only:
                continue
            out_mp4 = os.path.join(job_dir, f"{brain.slug(cid)}.mp4")
            print(f"[{i}/{len(clips)}] {cid} -> {os.path.basename(out_mp4)}")
            pad = float(clip.get("pad", 0.15))
            gw = clip.get("graphic_windows")
            if gw:  # fit the whole frame during on-screen graphics
                engine.render_framed(video, build_segments(clip["ranges"], gw, pad), out_mp4)
            else:
                engine.render_clip(video, clip["ranges"], out_mp4,
                                   layout=clip.get("layout"), pad=pad)
            out.append({
                "id": cid,
                "hook": clip.get("hook", ""),
                "why": clip.get("why", ""),
                "hook_score": clip.get("hook_score"),
                "body_score": clip.get("body_score"),
                "score": clip.get("score"),
                "score_reason": clip.get("score_reason", ""),
                "url": f"/jobs/{job_id}/{os.path.basename(out_mp4)}",
            })

        if not only:
            set_status(status="done", message="", clips=out)
        print(f"render ok: {len(out)} clip(s)")
    except Exception as e:
        if not only:
            set_status(status="error", message=f"{e}")
        print("render FAILED:\n" + traceback.format_exc(), file=sys.stderr)
        sys.exit(1)


# ============================================================================
# caption - burn clean subtitles synced to the transcript words
# ============================================================================

CAP_STYLE = (
    "Style: Cap,Nirmala UI,64,&H00FFFFFF,&H000000FF,&H00000000,&H90000000,"
    "-1,0,0,0,100,100,0,0,1,5,1,2,80,80,600,1"
)


def _ass_time(t):
    t = max(0.0, t)
    h = int(t // 3600); t -= h * 3600
    m = int(t // 60); t -= m * 60
    s = int(t); cs = int(round((t - s) * 100))
    if cs == 100:
        cs = 0; s += 1
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _load_words(job_dir):
    rows = []
    with open(os.path.join(job_dir, "words.tsv"), encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 3:
                rows.append((float(parts[0]), float(parts[1]), parts[2]))
    return rows


def _clip_local_words(words, ranges, pad):
    """Map source-time words into the stitched clip's local timeline."""
    out, offset = [], 0.0
    for s, e in ranges:
        s = float(s); e = float(e)
        seg_start = s - pad          # engine seeks here
        for ws, we, tok in words:
            mid = (ws + we) / 2.0
            if s <= mid <= e:
                out.append((offset + (ws - seg_start),
                            offset + (we - seg_start), tok.strip()))
        offset += (e + pad) - seg_start
    return out


_PUNCT = ",।.!?-—:;"


def _group_cues(lw, max_words=4, max_span=1.8, max_gap=0.5):
    cues, cur = [], []
    enders = ("।", ".", "!", "?")
    for w in lw:
        tok = w[2].strip()
        if not tok or all(ch in _PUNCT for ch in tok):
            continue  # skip empty / punctuation-only tokens
        if not cur:
            cur = [w]; continue
        span = w[1] - cur[0][0]
        gap = w[0] - cur[-1][1]
        prev_end = cur[-1][2].endswith(enders)
        if len(cur) >= max_words or span > max_span or gap > max_gap or prev_end:
            cues.append(cur); cur = [w]
        else:
            cur.append(w)
    if cur:
        cues.append(cur)
    dialogues = []
    for i, cue in enumerate(cues):
        start = cue[0][0]
        end = cue[-1][1] + 0.20
        if i + 1 < len(cues):
            end = min(end, cues[i + 1][0][0] - 0.01)
        text = " ".join(w[2].strip() for w in cue)
        text = " ".join(text.split())                 # collapse whitespace
        # strip leading spaces / punctuation / zero-width marks (keep letters, any script)
        text = re.sub(r"^[\s\W_]+", "", text)
        text = text.replace("{", "(").replace("}", ")")
        if end > start and text:
            dialogues.append((start, end, text))
    return dialogues


def _write_ass(path, dialogues):
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, "
        "SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
        "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + CAP_STYLE + "\n\n[Events]\nFormat: Layer, Start, End, Style, Name, "
        "MarginL, MarginR, MarginV, Effect, Text\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(head)
        for start, end, text in dialogues:
            f.write(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Cap,,0,0,0,,{text}\n")


def cmd_caption(job_dir):
    with open(os.path.join(job_dir, "clips.json"), encoding="utf-8-sig") as f:
        spec = json.load(f)
    clips = spec.get("clips", [])
    video = _find_video(job_dir)
    stem = os.path.splitext(os.path.basename(video))[0]
    words = _load_words(job_dir)

    only = sys.argv[3] if len(sys.argv) > 3 else None
    for i, clip in enumerate(clips, 1):
        cid = clip.get("id", f"clip{i}")
        if only and cid != only:
            continue
        pad = float(clip.get("pad", 0.15))
        lw = _clip_local_words(words, clip["ranges"], pad)
        dialogues = _group_cues(lw)
        if os.environ.get("HINGLISH", "1") != "0":   # romanize Hindi -> Hinglish
            texts = romanize.romanize_lines([d[2] for d in dialogues])
            dialogues = [(d[0], d[1], t) for d, t in zip(dialogues, texts)]
        sid = brain.slug(cid)
        ass_name = f"{sid}.ass"
        _write_ass(os.path.join(job_dir, ass_name), dialogues)

        clip_mp4 = f"{sid}.mp4"
        tmp_mp4 = f"{sid}.cap.mp4"
        # run inside job_dir so libass gets a simple relative filename
        cmd = ["ffmpeg", "-y", "-i", clip_mp4,
               "-vf", f"subtitles=filename={ass_name}",  # explicit opt name: version-proof
               "-c:v", "libx264", "-preset", "medium", "-crf", "20",
               "-c:a", "copy", "-movflags", "+faststart", tmp_mp4]
        print(f"[caption] {cid}: {len(dialogues)} cues")
        p = subprocess.run(cmd, cwd=job_dir, capture_output=True, text=True)
        if p.returncode != 0:
            print(p.stderr[-1500:], file=sys.stderr)
            sys.exit(f"caption ffmpeg failed for {cid}")
        os.replace(os.path.join(job_dir, tmp_mp4),
                   os.path.join(job_dir, clip_mp4))
    print("caption ok")


if __name__ == "__main__":
    cmds = {"prep": cmd_prep, "render": cmd_render, "caption": cmd_caption}
    if len(sys.argv) < 3 or sys.argv[1] not in cmds:
        sys.exit("usage: python job_tools.py [prep|render|caption] <job_dir> [clip_id]")
    cmds[sys.argv[1]](sys.argv[2])
