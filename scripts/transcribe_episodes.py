#!/usr/bin/env python3
"""
One-off script: transcribe existing podcast MP3s → podcast_epNNN.txt
Uses Groq Whisper API (same GROQ_API_KEY already in use).
Run once from the repo root:
    python scripts/transcribe_episodes.py
"""

import os
import sys
from pathlib import Path

import requests

GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
DATA_DIR = Path("data")


def transcribe_mp3(mp3_path: Path, api_key: str) -> str:
    """Send MP3 to Groq Whisper, return transcript text."""
    with open(mp3_path, "rb") as f:
        r = requests.post(
            GROQ_WHISPER_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (mp3_path.name, f, "audio/mpeg")},
            data={
                "model": "whisper-large-v3-turbo",
                "response_format": "text",
                "language": "en",
            },
            timeout=120,
        )
    r.raise_for_status()
    # response_format=text returns the transcript directly as plain text
    return r.text.strip()


def main() -> int:
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        print("ERROR: GROQ_API_KEY not set")
        return 1

    mp3_files = sorted(DATA_DIR.glob("podcast_ep*.mp3"))
    if not mp3_files:
        print("No MP3 files found in data/")
        return 1

    print(f"Found {len(mp3_files)} MP3(s) to transcribe\n")

    for mp3_path in mp3_files:
        txt_path = mp3_path.with_suffix(".txt")

        if txt_path.exists():
            print(f"  SKIP {mp3_path.name} — {txt_path.name} already exists")
            continue

        size_mb = mp3_path.stat().st_size / 1_048_576
        print(f"  Transcribing {mp3_path.name} ({size_mb:.1f} MB)...", end=" ", flush=True)
        try:
            transcript = transcribe_mp3(mp3_path, api_key)
            txt_path.write_text(transcript, encoding="utf-8")
            word_count = len(transcript.split())
            print(f"✓  {word_count:,} words → {txt_path.name}")
        except Exception as exc:
            print(f"✗  FAILED: {exc}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
