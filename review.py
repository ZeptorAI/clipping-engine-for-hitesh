#!/usr/bin/env python3
"""
Quality gate for the tightening engine.

The cut itself is deterministic (tighten.py) and never checks meaning. It can
delete a whole clause and every metric still looks perfect, because from the
arithmetic's point of view it removed audio it believed was a duplicate.

This pass shows Claude BOTH the original transcript and the surviving script and
asks what the cut broke. It flags spans for a human to check; it does not edit.

review(transcript_path, keeps, model=None) -> (flags, usage)
Key read from .env as ANTHROPIC_API_KEY. Model from REVIEW_MODEL.
"""

import json
import os

import brain
import tighten

DEFAULT_REVIEW_MODEL = "claude-sonnet-5"
MAX_TOKENS = 32000   # headroom over adaptive thinking; see autofix.MAX_TOKENS

SYSTEM = """You are proofreading an automated video edit of a Hinglish (Hindi + \
English) voiceover.

The editor removed silences and repeated takes. It works on word timings only and \
has NO understanding of meaning, so its typical failures are:
  - deleting BOTH takes of a line, so the point vanishes entirely
  - leaving a clause without its head (e.g. "...hai ki X" where "Problem ye" was cut)
  - keeping the take that trails off unfinished instead of the complete one
  - flattening a genuine Hindi reduplication into one word. "kabhi kabhi"
    (sometimes) and "dhire dhire" (gradually) are REAL words, not stutters
  - leaving a sub-word stutter fragment in ("con-", "stud-", "fo-")
  - keeping two takes that say the same thing, so a line is duplicated

You will get the ORIGINAL spoken transcript and the CUT script that survived.

Report only real damage. Do NOT flag:
  - deliberately removed repeated takes (that is the entire point of the tool)
  - a line being short, or a sentence split across consecutive lines - the lines
    play back-to-back as continuous audio
  - informal or spoken-register grammar; this is natural speech, not writing

Return ONLY JSON:
{"flags":[{"line":<int>,"issue":"<what broke, one sentence>",
           "fix":"<what to restore or drop, be specific>",
           "severity":"high"|"medium"}]}
"high" = meaning lost or sentence broken. "medium" = clumsy but understandable.
Empty list if the cut is clean."""


def _original_text(words):
    return " ".join(w["raw"].strip() for w in words)


def review(transcript_path, keeps, model=None):
    """Flag spans where the cut broke meaning. Returns (flags, usage)."""
    words = tighten.load_words(transcript_path)
    lines = tighten.cut_lines(words, keeps)

    cut_txt = "\n".join("%d. [%.2f-%.2f] %s" % (l["line"], l["start"], l["end"],
                                                l["text"] or "(no words)")
                        for l in lines)
    user = ("ORIGINAL TRANSCRIPT (everything that was said, including the repeated "
            "takes):\n%s\n\n"
            "CUT SCRIPT (what survived, numbered; these lines play back-to-back):\n%s\n\n"
            "Which numbered lines show real damage?" % (_original_text(words), cut_txt))

    client = brain._client()
    model = model or os.environ.get("REVIEW_MODEL") or DEFAULT_REVIEW_MODEL
    text, usage = brain._call(client, SYSTEM, user, model=model,
                              max_tokens=MAX_TOKENS)
    # Truncated or unparseable used to return zero flags, which reads as
    # "the cut is clean" - the one answer we must never fake.
    if (usage or {}).get("stop_reason") == "max_tokens":
        raise RuntimeError("review was cut off at the %d-token budget"
                           % MAX_TOKENS)
    try:
        data = brain._parse_json(text)
    except Exception as e:
        raise RuntimeError("could not read the review (%s): %.200s"
                           % (e.__class__.__name__, text))

    by_line = {l["line"]: l for l in lines}
    flags = []
    for f in (data.get("flags") or []):
        n = f.get("line")
        src = by_line.get(n if isinstance(n, int) else -1)
        if not src:
            continue
        flags.append({
            "line": n,
            "start": src["start"],
            "end": src["end"],
            "text": src["text"],
            "issue": (f.get("issue") or "").strip(),
            "fix": (f.get("fix") or "").strip(),
            "severity": "high" if str(f.get("severity", "")).lower() == "high" else "medium",
        })
    flags.sort(key=lambda x: (x["severity"] != "high", x["start"]))
    return flags, usage


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        sys.exit("usage: python review.py <transcript.json> <keeps.json>")
    keeps = [tuple(x) for x in json.load(open(sys.argv[2]))]
    fl, u = review(sys.argv[1], keeps)
    print("%d flag(s)  |  %s  $%.4f" % (len(fl), u["model"], u["cost_usd"]))
    for f in fl:
        print("\n[%s] line %d  %.2f-%.2f\n  %s\n  ISSUE: %s\n  FIX:   %s"
              % (f["severity"].upper(), f["line"], f["start"], f["end"],
                 f["text"][:90], f["issue"], f["fix"]))
