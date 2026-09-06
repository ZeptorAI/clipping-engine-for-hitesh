# -*- coding: utf-8 -*-
"""Self-driving retake + dead-air remover for long-form talking-head media.

Two layers:
  SEMANTIC  - word timings decide WHAT is said; false starts / retakes are peeled
              so only the final clean take of each phrase survives.
  ACOUSTIC  - ffmpeg silencedetect decides WHERE sound actually is, so silence
              hiding inside an over-long ASR word is removed too.

Usage:  python tighten.py <media> <transcript.json> <out>
Works on audio-only or A/V input (auto-detected).
"""
import io, json, os, re, subprocess, sys

PUNCT = u"।,.?!:;\"'()[]—–-…“”‘’|/\\ "

# ---- pacing knobs ----
KEEP_GAP   = 0.35     # transcript-level: pause longer than this is a cut candidate
K_WINDOW   = 22       # look-ahead (tokens) for a retake anchor
FRAC       = 0.55     # retake must re-say >=55% of the aborted attempt (LCS)
SPEECH_PAD = 0.045    # air kept each side of a speech burst (protects plosives)
PAD2       = 0.030    # tighter pad on the verified 2nd pass
MIN_REMOVE = 0.060
MIN_SEG    = 0.050
SIL_OFFSET = 5.0      # silence threshold sits this far below the file's mean level
SIL_MIN    = 0.08


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def probe(media):
    r = sh('ffprobe -v error -show_entries format=duration '
           '-of default=noprint_wrappers=1:nokey=1 "%s"' % media)
    dur = float(r.stdout.strip().splitlines()[0])
    r = sh('ffprobe -v error -select_streams v:0 -show_entries stream=codec_type '
           '-of default=noprint_wrappers=1:nokey=1 "%s"' % media)
    return dur, ("video" in r.stdout)


def silence_threshold(media):
    """Pick the silencedetect threshold from the file's own loudness.

    A mastered/normalised VO can sit 13 dB hotter than a raw one; a fixed -35 dB
    then finds almost no silence and the whole acoustic pass silently no-ops.
    Anchoring to mean_volume keeps behaviour identical across both."""
    r = sh('ffmpeg -hide_banner -nostats -i "%s" -af volumedetect -f null - 2>&1' % media)
    m = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", r.stdout + r.stderr)
    mean = float(m.group(1)) if m else -30.0
    return round(abs(mean - SIL_OFFSET), 1)


def norm(t):
    return t.strip().strip(PUNCT).strip().lower()


def load_words(path):
    d = json.load(open(path, encoding="utf-8"))
    out = []
    for s in d["segments"]:
        for w in s["words"]:
            # ASR non-speech annotations ("[clears throat]", "[clap]") are not words.
            # Keeping them would anchor a breath-blip and skew retake detection.
            if w["text"].strip().startswith("["):
                continue
            n = norm(w["text"])
            if n:
                out.append({"raw": w["text"], "n": n,
                            "s": float(w["start_time"]), "e": float(w["end_time"])})
    return out


def lcs_len(a, b):
    dp = [0] * (len(b) + 1)
    for x in range(len(a) - 1, -1, -1):
        prev = 0
        for y in range(len(b) - 1, -1, -1):
            tmp = dp[y]
            dp[y] = (1 + prev) if a[x] == b[y] else max(dp[y], dp[y + 1])
            prev = tmp
    return dp[0]


def detect_repeats(words):
    """Peel false starts: keep the final take, delete the aborted attempt."""
    ntok = [w["n"] for w in words]
    N = len(words)
    deleted = [False] * N
    spans = []
    i = 0
    while i < N:
        if deleted[i]:
            i += 1
            continue
        best = None
        for k in range(1, min(K_WINDOW, N - i - 1) + 1):
            if ntok[i + k] != ntok[i]:
                continue
            A = ntok[i:i + k]
            R = ntok[i + k:min(N, i + 2 * k + 2)]
            if k == 1:
                lcs = 1
            else:
                lcs = lcs_len(A, R)
                if lcs < 2 or lcs < FRAC * k:
                    continue
            if best is None or lcs > best[0] or (lcs == best[0] and k < best[1]):
                best = (lcs, k)
        if best:
            k = best[1]
            for j in range(i, i + k):
                deleted[j] = True
            spans.append((i, i + k))
            i += k
            continue
        i += 1
    return deleted, spans


def keep_intervals(words, deleted):
    kept = [k for k in range(len(words)) if not deleted[k]]
    ivs, cur = [], None
    for k in kept:
        w = words[k]
        if cur is None:
            cur = [w["s"], w["e"], k]
            continue
        if k == cur[2] + 1 and w["s"] - words[cur[2]]["e"] <= KEEP_GAP:
            cur[1], cur[2] = w["e"], k
        else:
            ivs.append([cur[0], cur[1]])
            cur = [w["s"], w["e"], k]
    if cur:
        ivs.append([cur[0], cur[1]])
    return ivs


def apply_overrides(ivs, ovr):
    """Hand-picked fixes for dense retake clusters the auto peeler mis-reads.
    Any auto interval overlapping a window is dropped and replaced by its keeps."""
    wins = [o["win"] for o in ovr]
    out = [iv for iv in ivs
           if not any(w[0] < iv[1] and iv[0] < w[1] for w in wins)]
    for o in ovr:
        out.extend([list(k) for k in o.get("keeps", [])])
    out.sort()
    return out


def silence_map(media, sp, tag, db):
    f = os.path.join(sp, "sil_%s_%s.txt" % (tag, db))
    if not os.path.exists(f):
        r = sh('ffmpeg -hide_banner -nostats -i "%s" -af '
               '"silencedetect=noise=-%sdB:d=%s" -f null - 2>&1'
               % (media, db, SIL_MIN))
        vals = re.findall(r"silence_(?:start|end): ([0-9.]+)", r.stdout + r.stderr)
        open(f, "w").write("\n".join(vals))
    v = [float(x) for x in open(f).read().split()]
    return [(v[i], v[i + 1]) for i in range(0, len(v) - 1, 2)]


def subtract(keeps, cuts, pad):
    trimmed = sorted((a + pad, b - pad) for a, b in cuts
                     if (b - pad) - (a + pad) >= MIN_REMOVE)
    out = []
    for ks, ke in keeps:
        cur = ks
        for ca, cb in trimmed:
            if cb <= cur or ca >= ke:
                continue
            if ca > cur:
                out.append([cur, min(ca, ke)])
            cur = max(cur, cb)
            if cur >= ke:
                break
        if cur < ke:
            out.append([cur, ke])
    return [iv for iv in out if iv[1] - iv[0] >= MIN_SEG]


def clean(ivs, end, quant):
    out = []
    for s, e in ivs:
        s2 = max(0.0, round(s / quant) * quant)
        e2 = min(end, round(e / quant) * quant)
        if e2 - s2 >= 2 * quant:
            out.append([round(s2, 4), round(e2, 4)])
    merged = []
    for iv in out:
        if merged and iv[0] <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], iv[1])
        else:
            merged.append(iv)
    return merged


def merge_micro(ivs, max_gap=0.08):
    """Rejoin keeps split by a sub-80ms gap. That gap sits INSIDE a word (a stop
    consonant reads as silence), so cutting there clips the word in two."""
    out = []
    for s, e in ivs:
        if out and s - out[-1][1] <= max_gap:
            out[-1][1] = e
        else:
            out.append([s, e])
    return out


def drop_blips(ivs, words, max_dur=0.25):
    """Remove short segments that contain no actual word - breaths, clicks, room noise.
    A genuinely short word (e.g. a 0.08s "or") overlaps a token and is kept."""
    out = []
    for s, e in ivs:
        if (e - s) < max_dur:
            # A real word (even a 0.08s one) begins inside the segment. A breath
            # sitting in the trailing silence of an over-long ASR word does not.
            hit = any(s <= w["s"] <= e for w in words)
            if not hit:
                continue
        out.append([s, e])
    return out


def map_back(keeps, out_sil):
    """Silence found in the RENDERED output -> source-time spans."""
    starts, t = [], 0.0
    for s, e in keeps:
        starts.append(t)
        t += e - s
    cuts = []
    for a, b in out_sil:
        for i, (s, e) in enumerate(keeps):
            lo = max(a, starts[i])
            hi = min(b, starts[i] + (e - s))
            if hi > lo:
                cuts.append((s + (lo - starts[i]), s + (hi - starts[i])))
    return cuts


def filtergraph(keeps, has_video, path):
    parts, labels = [], []
    for i, (s, e) in enumerate(keeps):
        d = e - s
        f = min(0.008, d / 4)
        if has_video:
            parts.append("[0:v]trim=start=%.4f:end=%.4f,setpts=PTS-STARTPTS[v%d]" % (s, e, i))
        parts.append("[0:a]atrim=start=%.4f:end=%.4f,asetpts=PTS-STARTPTS,"
                     "afade=t=in:st=0:d=%.4f,afade=t=out:st=%.4f:d=%.4f[a%d]"
                     % (s, e, f, d - f, f, i))
        labels.append(("[v%d]" % i if has_video else "") + "[a%d]" % i)
    parts.append("".join(labels) +
                 "concat=n=%d:v=%d:a=1" % (len(keeps), 1 if has_video else 0) +
                 ("[outv][outa]" if has_video else "[outa]"))
    open(path, "w", encoding="utf-8").write(";\n".join(parts))


_FILTER_FLAG = None


def filter_flag():
    """How this ffmpeg accepts a filtergraph from a file.

    The graph runs to tens of KB - well past a command-line length limit - so it
    has to come from a file. ffmpeg >= 7 spells that `-/filter_complex FILE`;
    6.x (Ubuntu 24.04 ships 6.1) only has `-filter_complex_script FILE`. Neither
    build accepts the other's spelling, so pick by version.
    """
    global _FILTER_FLAG
    if _FILTER_FLAG is None:
        r = sh("ffmpeg -hide_banner -version")
        m = re.search(r"ffmpeg version n?(\d+)", (r.stdout or "") + (r.stderr or ""))
        _FILTER_FLAG = ("-/filter_complex" if (m and int(m.group(1)) >= 7)
                        else "-filter_complex_script")
    return _FILTER_FLAG


def render(media, fg, out, has_video):
    maps = '-map "[outv]" -map "[outa]"' if has_video else '-map "[outa]"'
    if has_video:
        codec = ('-c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p '
                 '-c:a aac -b:a 192k -movflags +faststart')
    elif out.lower().endswith(".wav"):
        codec = "-c:a pcm_s16le"
    else:
        codec = "-c:a aac -b:a 192k"
    def run(flag):
        return sh('ffmpeg -y -hide_banner -loglevel error -i "%s" %s "%s" %s %s "%s"'
                  % (media, flag, fg, maps, codec, out))

    r = run(filter_flag())
    if r.returncode != 0 and "Unrecognized option" in ((r.stderr or "") + (r.stdout or "")):
        # version sniff was wrong for this build - try the other spelling once
        global _FILTER_FLAG
        _FILTER_FLAG = ("-filter_complex_script" if _FILTER_FLAG == "-/filter_complex"
                        else "-/filter_complex")
        r = run(_FILTER_FLAG)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg render failed: "
                           + ((r.stderr or r.stdout or "")[-600:]))


def tighten(media, transcript, out, overrides=None, workdir=None, progress=None):
    """Cut retakes + dead air. Returns a stats dict.

    workdir  : where scratch files go (defaults next to the output)
    progress : optional callable(str) for UI stage messages
    """
    def say(m):
        if progress:
            progress(m)

    ovr = overrides or []
    sp = workdir or os.path.dirname(os.path.abspath(out)) or "."
    os.makedirs(sp, exist_ok=True)
    tag = os.path.splitext(os.path.basename(media))[0]
    end, has_video = probe(media)
    quant = 1.0 / 25 if has_video else 0.001

    words = load_words(transcript)
    if not words:
        raise RuntimeError("Transcript has no usable word timings.")
    deleted, spans = detect_repeats(words)
    sem = apply_overrides(keep_intervals(words, deleted), ovr)

    # pass 1 - subtract measured silence (threshold adapts to this file's level)
    db = silence_threshold(media)
    say("Measuring silence (-%s dB)..." % db)
    sil = silence_map(media, sp, tag, db)
    p1 = clean(subtract(sem, sil, SPEECH_PAD), end, quant)
    if not p1:
        raise RuntimeError("Nothing left to keep - check the transcript matches the media.")

    # pass 2 - render, re-measure, map residual silence back to source, subtract
    ext = ".mp4" if has_video else ".wav"
    fg1 = os.path.join(sp, "fg_%s_p1.txt" % tag)
    tmp = os.path.join(sp, "tmp_%s_p1%s" % (tag, ext))
    say("First pass render...")
    filtergraph(p1, has_video, fg1)
    render(media, fg1, tmp, has_video)
    say("Verifying against the rendered audio...")
    res = silence_map(tmp, sp, tag + "_res", db)
    final = drop_blips(
        merge_micro(clean(subtract(p1, map_back(p1, res), PAD2), end, quant)), words)

    say("Final render...")
    fg2 = os.path.join(sp, "fg_%s_final.txt" % tag)
    filtergraph(final, has_video, fg2)
    render(media, fg2, out, has_video)
    try:
        os.remove(tmp)
    except OSError:
        pass

    # speech-preservation check: total kept minus overlap with the silence map.
    # If this barely moves between sem and final, we cut silence, not words.
    def overlap(ivs):
        return sum(max(0.0, min(b, e) - max(a, s)) for a, b in ivs for s, e in sil)

    def total(ivs):
        return sum(b - a for a, b in ivs)

    sem_speech = total(sem) - overlap(sem)
    fin_speech = total(final) - overlap(final)
    dur = total(final)

    stats = {
        "source_sec": round(end, 2),
        "output_sec": round(dur, 2),
        "kept_pct": round(dur / end * 100),
        "segments": len(final),
        "retake_cuts": len(spans),
        "words_deleted": int(sum(deleted)),
        "words_total": len(words),
        "silence_db": db,
        "speech_before_sec": round(sem_speech, 1),
        "speech_after_sec": round(fin_speech, 1),
        "speech_kept_pct": round(fin_speech / sem_speech * 100, 1) if sem_speech else 0.0,
        "keeps": final,
    }
    json.dump(final, open(os.path.join(sp, "keep_%s.json" % tag), "w"), indent=0)
    return stats


def cut_lines(words, keeps):
    """Rebuild the surviving script: one line per kept segment.

    A word counts as audible if its onset survived - sentence-final words carry a
    long trailing silence inside their ASR span, so coverage alone under-reports
    them. Non-speech tags were already dropped by load_words."""
    owner = {}
    for w in words:
        span = w["e"] - w["s"]
        cov = sum(max(0.0, min(w["e"], e) - max(w["s"], s)) for s, e in keeps)
        onset = any(s - 0.02 <= w["s"] <= e for s, e in keeps)
        if not onset and (span <= 0 or cov < 0.5 * span):
            continue
        best_i, best_ov = None, 0.0
        for i, (s, e) in enumerate(keeps):
            ov = min(w["e"], e) - max(w["s"], s)
            if ov > best_ov:
                best_i, best_ov = i, ov
        if best_i is None:
            for i, (s, e) in enumerate(keeps):
                if s - 0.02 <= w["s"] <= e:
                    best_i = i
                    break
        if best_i is not None:
            owner.setdefault(best_i, []).append(w["raw"].strip())
    return [{"line": i + 1, "start": round(s, 2), "end": round(e, 2),
             "text": " ".join(owner.get(i, []))}
            for i, (s, e) in enumerate(keeps)]


def main():
    if len(sys.argv) < 4:
        sys.exit("usage: python tighten.py <media> <transcript.json> <out> "
                 "[--overrides f.json]")
    media, tj, out = sys.argv[1], sys.argv[2], sys.argv[3]
    ovr = []
    if "--overrides" in sys.argv:
        ovr = json.load(open(sys.argv[sys.argv.index("--overrides") + 1], encoding="utf-8"))
    # scratch lands beside the output, not in the repo
    st = tighten(media, tj, out, ovr, progress=lambda m: print(m))
    print("SOURCE %.1fs -> OUTPUT %.1fs (%d%%)  %d segments"
          % (st["source_sec"], st["output_sec"], st["kept_pct"], st["segments"]))
    print("retake cuts: %d | words deleted: %d/%d | speech kept: %.1f%%"
          % (st["retake_cuts"], st["words_deleted"], st["words_total"],
             st["speech_kept_pct"]))


if __name__ == "__main__":
    main()
