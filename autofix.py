#!/usr/bin/env python3
"""
Turn review flags into override windows that actually change the cut.

review.py only reports damage. This converts each flagged span into the same
{"win": [...], "keeps": [...]} override I used to hand-write, validates it
against the semantic intervals, and hands it back for a re-cut.

An override is destructive by design: EVERY auto keep-interval overlapping
`win` is dropped and replaced by `keeps`. So a window whose edge lands inside
an interval silently deletes the part outside the window - that is the failure
mode validate() exists to catch, and it is not theoretical: it ate five real
sentences the first time I wrote overrides by hand.

propose(transcript, keeps, flags)  -> (overrides, usage)
validate(overrides, sem, end)      -> (safe_overrides, rejected)
better(new_flags, old_flags)       -> bool
"""

import json
import os

import brain
import tighten

DEFAULT_MODEL = "claude-sonnet-5"
CTX_PAD = 6.0        # seconds of word context shown either side of a flag
MIN_KEEP = 0.05      # a keep shorter than this is not a real span

SYSTEM = """You repair an automated Hinglish (Hindi + English) voiceover edit.

A deterministic cutter removed silences and repeated takes. It has no
understanding of meaning, so it sometimes deletes a clause that was never a
retake, keeps a take that trails off unfinished, or leaves a stutter fragment.
Someone has already flagged what broke. Your job is to express each repair as
an override.

OVERRIDE SEMANTICS - read carefully, this is destructive:
  {"win": [A, B], "keeps": [[s1,e1], [s2,e2], ...]}
  Every auto keep-interval that OVERLAPS [A,B] is DELETED, then `keeps` are
  inserted in its place.

Therefore:
  - [A,B] must FULLY CONTAIN every interval it touches. If an interval runs
    68.72-70.28 and you write A=69.5, that whole interval dies and you lose
    68.72-69.5 as well. Put A and B inside the GAPS between intervals - the
    listing gives you those gaps explicitly.
  - `keeps` must re-list every span inside the window you still want, including
    audio that was already correct. Anything you omit is gone.
  - Use timings from the word list. Start a keep at a word's start time and end
    it at a word's end time.
  - To simply delete a stray fragment, give the window and an empty keeps list.

Only propose a repair you are confident in. Skipping a flag is fine and better
than a wrong window - omit it. Never invent times outside the listed range.

Return ONLY JSON:
{"overrides":[{"line":<flag line>,"win":[A,B],"keeps":[[s,e],...],
               "why":"<one sentence>"}]}
Empty list if nothing can be repaired safely."""


def _context(words, deleted, sem, flag, pad=CTX_PAD):
    """Word-level context around one flag, plus the intervals a window must respect."""
    a, b = flag["start"] - pad, flag["end"] + pad
    lines = []
    for i, w in enumerate(words):
        if w["s"] >= a and w["e"] <= b:
            lines.append("    %-4s %8.2f-%8.2f  %s"
                         % ("DEL" if deleted[i] else "keep", w["s"], w["e"],
                            w["raw"].strip()))
    near = [(s, e) for s, e in sem if e > a and s < b]
    iv = "\n".join("    [%8.2f - %8.2f]" % (s, e) for s, e in near) or "    (none)"

    gaps = []
    for j in range(len(near) - 1):
        gaps.append("    %.2f .. %.2f" % (near[j][1], near[j + 1][0]))
    if near:
        gaps.insert(0, "    before %.2f" % near[0][0])
        gaps.append("    after %.2f" % near[-1][1])
    gp = "\n".join(gaps) or "    (none)"

    return (
        "FLAG line %d  [%.2f-%.2f]  severity=%s\n"
        "  issue: %s\n  suggested fix: %s\n"
        "  words nearby (keep = survived the cut, DEL = removed as a retake):\n%s\n"
        "  auto keep-intervals your window would replace:\n%s\n"
        "  SAFE window edges (gaps between those intervals):\n%s\n"
        % (flag["line"], flag["start"], flag["end"], flag["severity"],
           flag["issue"], flag.get("fix", ""), "\n".join(lines) or "    (none)", iv, gp))


def propose(transcript_path, flags, model=None):
    """Ask for an override per flag. Returns (overrides, usage)."""
    if not flags:
        return [], None
    words = tighten.load_words(transcript_path)
    deleted, _ = tighten.detect_repeats(words)
    sem = tighten.keep_intervals(words, deleted)

    blocks = [_context(words, deleted, sem, f) for f in flags]
    user = ("Repair these %d flagged spans.\n\n%s\n"
            "Give one override per flag you can safely repair."
            % (len(flags), "\n".join(blocks)))

    client = brain._client()
    model = model or os.environ.get("AUTOFIX_MODEL") or DEFAULT_MODEL
    text, usage = brain._call(client, SYSTEM, user, model=model)
    try:
        data = brain._parse_json(text)
    except Exception:
        return [], usage

    out = []
    for o in (data.get("overrides") or []):
        try:
            win = [float(o["win"][0]), float(o["win"][1])]
            keeps = [[float(s), float(e)] for s, e in (o.get("keeps") or [])]
        except Exception:
            continue
        out.append({"line": o.get("line"), "win": win, "keeps": keeps,
                    "why": (o.get("why") or "").strip()})
    return out, usage


def validate(overrides, sem, end, words=None):
    """Drop any override that would damage the cut. Returns (safe, rejected)."""
    safe, rejected = [], []
    for o in overrides:
        a, b = o["win"]
        why = None
        if not (0 <= a < b <= end + 0.01):
            why = "window outside the media (%.2f-%.2f)" % (a, b)
        # the check that matters: a window must not bisect an interval
        if why is None:
            for s, e in sem:
                if s < b and a < e and (s < a - 1e-6 or e > b + 1e-6):
                    why = ("window cuts interval [%.2f-%.2f] in half - would "
                           "delete the part outside the window" % (s, e))
                    break
        if why is None:
            prev = None
            for s, e in sorted(o["keeps"]):
                if e - s < MIN_KEEP:
                    why = "keep [%.2f-%.2f] shorter than %.2fs" % (s, e, MIN_KEEP)
                elif s < a - 1e-6 or e > b + 1e-6:
                    why = "keep [%.2f-%.2f] falls outside its window" % (s, e)
                elif prev is not None and s < prev - 1e-6:
                    why = "keeps overlap or are unordered at %.2f" % s
                if why:
                    break
                prev = e
        # a keep with no word onset in it is silence, not restored speech
        if why is None and words:
            for s, e in o["keeps"]:
                if not any(s - 0.05 <= w["s"] <= e for w in words):
                    why = "keep [%.2f-%.2f] contains no word onset" % (s, e)
                    break
        if why:
            rejected.append(dict(o, reason=why))
        else:
            safe.append(o)
    return safe, rejected


def better(new_flags, old_flags):
    """Did the re-cut actually improve things? Used to decide accept vs revert."""
    hi = lambda fl: sum(1 for f in fl if f.get("severity") == "high")
    if hi(new_flags) != hi(old_flags):
        return hi(new_flags) < hi(old_flags)
    return len(new_flags) < len(old_flags)


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        sys.exit("usage: python autofix.py <transcript.json> <keeps.json>")
    import review as review_mod
    keeps = [tuple(x) for x in json.load(open(sys.argv[2]))]
    flags, u1 = review_mod.review(sys.argv[1], keeps)
    print("review: %d flag(s)  $%.4f" % (len(flags), (u1 or {}).get("cost_usd", 0)))
    ovr, u2 = propose(sys.argv[1], flags)
    print("proposed: %d override(s)  $%.4f" % (len(ovr), (u2 or {}).get("cost_usd", 0)))

    words = tighten.load_words(sys.argv[1])
    deleted, _ = tighten.detect_repeats(words)
    sem = tighten.keep_intervals(words, deleted)
    end = max(w["e"] for w in words)
    safe, bad = validate(ovr, sem, end, words)
    print("\nACCEPTED %d:" % len(safe))
    for o in safe:
        print("  line %s  win %.2f-%.2f  keeps %s\n     %s"
              % (o["line"], o["win"][0], o["win"][1],
                 [[round(s, 2), round(e, 2)] for s, e in o["keeps"]], o["why"]))
    print("\nREJECTED %d:" % len(bad))
    for o in bad:
        print("  line %s  win %.2f-%.2f\n     %s" % (o["line"], o["win"][0],
                                                     o["win"][1], o["reason"]))
