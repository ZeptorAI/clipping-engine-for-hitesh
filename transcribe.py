#!/usr/bin/env python3
"""
Auto-transcribe a video with ElevenLabs Speech-to-Text (Scribe).

Replaces the manual "upload to 11labs -> export JSON -> drop it in" step.
Extracts the audio, sends it to ElevenLabs, and writes a transcript.json in the
exact {language_code, segments:[{words:[{text,start_time,end_time}]}]} shape the
rest of the pipeline already reads.

transcribe(media_path, out_json) -> (language_code, n_words)
Key read from .env as ELEVENLABS_API_KEY.
"""

import json
import os
import subprocess
import tempfile

import brain  # reuse the .env loader

STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"


def _extract_audio(media_path):
    tmp = tempfile.mkdtemp(prefix="stt_")
    audio = os.path.join(tmp, "audio.mp3")
    # mono 16 kHz mp3 — plenty for STT, tiny upload
    subprocess.run(["ffmpeg", "-y", "-i", media_path, "-vn",
                    "-ac", "1", "-ar", "16000", "-b:a", "64k", audio],
                   capture_output=True)
    if not os.path.isfile(audio) or os.path.getsize(audio) == 0:
        raise RuntimeError("Could not extract audio from the video.")
    return audio


def transcribe(media_path, out_json):
    brain._load_env()
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise RuntimeError(
            "No ELEVENLABS_API_KEY found. Add it to web/.env, then retry.")
    model = os.environ.get("ELEVENLABS_MODEL", "scribe_v1")

    audio = _extract_audio(media_path)

    import requests
    with open(audio, "rb") as f:
        resp = requests.post(
            STT_URL,
            headers={"xi-api-key": key},
            data={"model_id": model, "timestamps_granularity": "word"},
            files={"file": ("audio.mp3", f, "audio/mpeg")},
            timeout=1800,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"ElevenLabs error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()

    # flatten the response's word list -> our normalized segment shape
    words = []
    for w in data.get("words", []):
        if w.get("type") != "word":     # skip spacing / audio_event tokens
            continue
        s = w.get("start")
        if s is None:
            continue
        e = w.get("end")
        words.append({"text": w.get("text", ""),
                      "start_time": float(s),
                      "end_time": float(e if e is not None else s)})
    if not words:
        raise RuntimeError("ElevenLabs returned no word-level timestamps.")

    segment = {"text": data.get("text", ""),
               "start_time": words[0]["start_time"],
               "end_time": words[-1]["end_time"],
               "speaker": "", "words": words}
    out = {"language_code": data.get("language_code", ""), "segments": [segment]}
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out["language_code"], len(words)


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        sys.exit("usage: python transcribe.py <video> <out.json>")
    lang, n = transcribe(sys.argv[1], sys.argv[2])
    print(f"transcribed: {n} words, language={lang}")
