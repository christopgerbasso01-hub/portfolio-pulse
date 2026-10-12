#!/usr/bin/env python3
"""Re-record an episode's audio from its saved transcript, without rewriting the script.

    python3 scripts/revoice_episode.py 20

Uses the same voice chain as the weekly run and updates the duration in podcast_meta.json.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_podcast as gp  # noqa: E402

ep = int(sys.argv[1])
txt = gp.DATA_DIR / f"podcast_ep{ep:03d}.txt"
mp3 = gp.DATA_DIR / f"podcast_ep{ep:03d}.mp3"
turns = gp.parse_script(txt.read_text(encoding="utf-8"))
print(f"Re-recording episode {ep}: {len(turns)} turns")
with tempfile.TemporaryDirectory() as d:
    engine = gp.synthesize_episode(turns, mp3, Path(d))
dur, secs = gp.audio_duration(mp3)
print(f"  ✓ {engine}: {dur}")

meta = json.loads(gp.PODCAST_META.read_text())
if meta.get("episode") == ep:
    meta["duration"], meta["duration_seconds"] = dur, secs
else:
    for a in meta.get("archive", []):
        if a.get("episode") == ep:
            a["duration"] = dur
gp.PODCAST_META.write_text(json.dumps(meta, indent=2))
