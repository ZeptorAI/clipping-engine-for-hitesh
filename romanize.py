#!/usr/bin/env python3
"""
Romanize caption lines to Hinglish: Hindi (Devanagari) words -> English letters
(इसको -> "issko", क्यों -> "kyun"), English words left exactly as-is.

romanize_lines(lines) -> list[str]  (same length; falls back to originals on error)

Cheap by design — uses ROMANIZE_MODEL (default claude-haiku-4-5). If a line has
no Devanagari it's returned unchanged without an API call.
"""

import json
import os
import re

import brain  # reuse _load_env / key handling

DEVANAGARI = re.compile(r"[ऀ-ॿ]")

SYSTEM = (
    "You romanize subtitle lines to natural Hinglish. For each input line: write "
    "any Hindi/Devanagari words in English letters the way Indians actually type "
    "them (इसको -> issko, क्यों -> kyun, नहीं -> nahi, बाल -> baal). Leave English "
    "words EXACTLY as they are. Keep punctuation and word order. Do not translate, "
    "do not add or drop words. Return ONLY JSON: {\"lines\": [...]} with the same "
    "number of lines, in the same order."
)


def romanize_lines(lines):
    if not lines:
        return lines
    # Only romanize if there's actually Devanagari somewhere
    if not any(DEVANAGARI.search(x or "") for x in lines):
        return lines

    try:
        brain._load_env()
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return lines
        model = os.environ.get("ROMANIZE_MODEL", "claude-haiku-4-5")
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        payload = json.dumps({"lines": lines}, ensure_ascii=False)
        resp = client.messages.create(
            model=model, max_tokens=8000, system=SYSTEM,
            messages=[{"role": "user", "content": payload}])
        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", "") == "text").strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text).strip()
        out = json.loads(text).get("lines", [])
        if isinstance(out, list) and len(out) == len(lines):
            return [str(x) for x in out]
    except Exception:
        pass
    return lines  # never break captions if romanization fails
