#!/usr/bin/env python3
"""
The brain: reads a job's transcript and picks + scores clips via the Claude API.

- pick_clips(job_dir)          -> (clips, usage)  choose + score clips
- recut_clip(job_dir, id, ...) -> (clip, usage)   revise one clip from an instruction

Learns from the creator's past ratings (feedback.jsonl) by feeding them back
into the prompt. Editing rules live in rulebook.md. Key from .env.
"""

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
FEEDBACK = os.path.join(HERE, "feedback.jsonl")

RATES = {
    "claude-opus-5":     (5.0, 25.0),
    "claude-opus-4-8":   (5.0, 25.0),
    "claude-sonnet-5":   (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5":  (1.0,  5.0),
    "claude-fable-5":    (10.0, 50.0),
}
DEFAULT_MODEL = "claude-opus-4-8"

OUTPUT_SPEC = """\
Return ONLY a JSON object, no prose, no markdown fences. Shape:
{
  "clips": [
    {
      "id": "short-kebab-slug",
      "hook": "the opening line / why it grabs (<= 12 words)",
      "why": "one sentence: why this is a strong standalone clip",
      "hook_score": 0-10 integer (do the first ~3 seconds grab attention?),
      "body_score": 0-10 integer (is the body valuable / insightful / complete?),
      "score": 0-10 integer (overall reel quality),
      "score_reason": "one short line explaining the score",
      "ranges": [[start_seconds, end_seconds]]
    }
  ]
}
Times are in SECONDS from the transcript. One range per clip normally; use
multiple ranges only to remove a stumble/repeat inside one clip.
"""


def _load_env():
    path = os.path.join(HERE, ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            val = v.strip().strip('"').strip("'")
            if val:
                os.environ[k.strip()] = val


def _rulebook():
    path = os.path.join(HERE, "rulebook.md")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return "Pick the strongest standalone moments as complete, hooky short clips."


def _feedback_context(limit=25):
    """Turn the creator's past ratings into a lesson block for the prompt."""
    if not os.path.isfile(FEEDBACK):
        return ""
    rows = []
    with open(FEEDBACK, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    rows = rows[-limit:]
    if not rows:
        return ""
    lines = []
    for r in rows:
        rating = r.get("rating")
        hook = (r.get("hook") or "").strip()
        note = (r.get("note") or "").strip()
        if rating is None or not hook:
            continue
        lines.append(f"- [{rating}/10] {hook}" + (f" — {note}" if note else ""))
    if not lines:
        return ""
    return ("\n\nThe creator has rated your PAST clips. Learn from this — make "
            "more clips like the high-rated ones and fewer like the low-rated "
            "ones, and take the notes seriously:\n" + "\n".join(lines))


def _load_words(job_dir):
    rows = []
    p = os.path.join(job_dir, "words.tsv")
    if not os.path.isfile(p):
        return rows
    with open(p, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 3:
                rows.append((float(parts[0]), float(parts[1])))
    return rows


def _snap(t, edges, window=0.6):
    best, bestd = t, window
    for e in edges:
        d = abs(e - t)
        if d < bestd:
            best, bestd = e, d
    return best


def _snap_ranges(clip, words):
    if not words:
        return
    starts = [s for s, _ in words]
    ends = [e for _, e in words]
    snapped = []
    for r in clip.get("ranges", []):
        if len(r) != 2:
            continue
        s = _snap(float(r[0]), starts)
        e = _snap(float(r[1]), ends)
        if e > s:
            snapped.append([round(s, 2), round(e, 2)])
    clip["ranges"] = snapped


def _parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    if not text.startswith("{"):
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)
    return json.loads(text)


def _client():
    _load_env()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "No ANTHROPIC_API_KEY found. Add it to web/.env "
            "(copy .env.example to .env and paste your key).")
    import anthropic
    return anthropic.Anthropic(api_key=key)


def _call(client, system, user, model=None):
    import anthropic
    model = model or os.environ.get("CLIP_MODEL") or DEFAULT_MODEL
    effort = os.environ.get("CLIP_EFFORT", "xhigh")
    kwargs = dict(model=model, max_tokens=16000, system=system,
                  messages=[{"role": "user", "content": user}])
    try:
        resp = client.messages.create(
            thinking={"type": "adaptive"},
            output_config={"effort": effort}, **kwargs)
    except anthropic.BadRequestError:
        resp = client.messages.create(**kwargs)
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    u = resp.usage
    in_tok = u.input_tokens + getattr(u, "cache_read_input_tokens", 0) \
        + getattr(u, "cache_creation_input_tokens", 0)
    out_tok = u.output_tokens
    in_rate, out_rate = RATES.get(model, (5.0, 25.0))
    cost = in_tok / 1e6 * in_rate + out_tok / 1e6 * out_rate
    usage = {"model": model, "input_tokens": in_tok,
             "output_tokens": out_tok, "cost_usd": round(cost, 4)}
    return text, usage


def _read(job_dir, name):
    with open(os.path.join(job_dir, name), encoding="utf-8") as f:
        return f.read()


def pick_clips(job_dir, max_clips=6):
    """Choose + score clips. Writes clips.json, returns (clips, usage)."""
    client = _client()
    transcript = _read(job_dir, "readable.txt")
    system = _rulebook() + _feedback_context() + "\n\n" + OUTPUT_SPEC
    user = ("Here is the timestamped transcript. Each line is [start-end] text."
            "\n\n" + transcript)

    text, usage = _call(client, system, user)
    data = _parse_json(text)
    clips = data.get("clips", [])[:max_clips]
    words = _load_words(job_dir)
    out = []
    for i, c in enumerate(clips, 1):
        c.setdefault("id", f"clip{i}")
        c.setdefault("pad", 0.08)
        _snap_ranges(c, words)
        if c.get("ranges"):
            out.append(c)

    with open(os.path.join(job_dir, "clips.json"), "w", encoding="utf-8") as f:
        json.dump({"clips": out}, f, ensure_ascii=False, indent=2)
    return out, usage


def recut_clip(job_dir, clip_id, instruction):
    """Revise a single clip from a plain-language instruction. Returns (clip, usage)."""
    client = _client()
    transcript = _read(job_dir, "readable.txt")
    with open(os.path.join(job_dir, "clips.json"), encoding="utf-8-sig") as f:
        spec = json.load(f)
    clips = spec.get("clips", [])
    cur = next((c for c in clips if c.get("id") == clip_id), None)
    if cur is None:
        raise RuntimeError(f"clip {clip_id} not found")

    system = (_rulebook() + "\n\n"
              "You are revising ONE existing clip based on the creator's "
              "instruction. Keep it a complete, self-contained story.\n\n"
              + OUTPUT_SPEC.replace('"clips": [', '"clip": ').replace("\n    }\n  ]\n}", "\n  }\n}"))
    user = (f"Full timestamped transcript:\n\n{transcript}\n\n"
            f"The current clip (id '{clip_id}') is:\n{json.dumps(cur, ensure_ascii=False)}\n\n"
            f"The creator's instruction: {instruction}\n\n"
            "Return the REVISED clip as JSON: {\"clip\": {...}} keeping the same id.")

    text, usage = _call(client, system, user)
    data = _parse_json(text)
    new = data.get("clip") or (data.get("clips") or [None])[0]
    if not new:
        raise RuntimeError("no revised clip returned")
    new["id"] = clip_id
    new.setdefault("pad", cur.get("pad", 0.08))
    _snap_ranges(new, _load_words(job_dir))
    if not new.get("ranges"):
        new["ranges"] = cur.get("ranges", [])

    for i, c in enumerate(clips):
        if c.get("id") == clip_id:
            clips[i] = new
            break
    with open(os.path.join(job_dir, "clips.json"), "w", encoding="utf-8") as f:
        json.dump({"clips": clips}, f, ensure_ascii=False, indent=2)
    return new, usage


def save_feedback(entry):
    """Append one rating to feedback.jsonl (used to learn next time)."""
    with open(FEEDBACK, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_feedback():
    if not os.path.isfile(FEEDBACK):
        return []
    out = []
    with open(FEEDBACK, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def learning_summary():
    """Ask the model to summarize what it's learned from the creator's ratings."""
    rows = [r for r in load_feedback() if r.get("rating") is not None]
    if not rows:
        return "No ratings yet — rate some clips and I'll show what I've learned.", None
    lines = [f"[{r.get('rating')}/10] hook: {r.get('hook','')} | note: {r.get('note') or '-'}"
             for r in rows]
    system = (
        "You cut short-form clips for a creator. Below are their ratings + notes on "
        "clips you made. In a short, punchy list (plain text, no markdown headers), "
        "summarize what you've learned about their taste — the hook styles, topics, "
        "lengths, and pacing they reward vs. punish — as concrete rules you'll apply "
        "next time. Be specific. If there isn't enough signal yet, say so honestly.")
    user = "Their ratings and notes:\n\n" + "\n".join(lines)
    text, usage = _call(_client(), system, user)
    with open(os.path.join(HERE, "learning_summary.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    return text, usage


def cached_summary():
    p = os.path.join(HERE, "learning_summary.txt")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return f.read()
    return ""


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        sys.exit("usage: python brain.py <job_dir>")
    clips, usage = pick_clips(sys.argv[1])
    print(f"picked {len(clips)} clips | {usage}")
