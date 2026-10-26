#!/usr/bin/env python3
"""
Render the Portfolio Pulse Weekly intro and outro themes.

Synthesised from scratch (no samples, no licences): a warm electric-piano chord
progression in F major, a soft bass, a light shaker pulse and a bell-like motif.
Written as 24 kHz mono 48 kbps MP3 — the same format edge-tts produces — so the
themes can be joined to the episode audio byte-for-byte.

    python3 scripts/make_theme.py      # writes scripts/assets/theme_intro.mp3, theme_outro.mp3
"""
from pathlib import Path

import lameenc
import numpy as np

SR = 24_000
BPM = 104
BEAT = 60 / BPM
OUT = Path(__file__).resolve().parent / "assets"


def hz(note: int) -> float:                     # MIDI note -> frequency
    return 440.0 * 2 ** ((note - 69) / 12)


def env(n: int, attack: float, decay: float) -> np.ndarray:
    t = np.arange(n) / SR
    a = np.clip(t / max(attack, 1e-4), 0, 1)
    return a * np.exp(-t / decay)


def epiano(note: int, dur: float, vel: float = 1.0) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = hz(note)
    tone = (np.sin(2 * np.pi * f * t) + 0.45 * np.sin(2 * np.pi * 2 * f * t) * np.exp(-t / 0.35)
            + 0.18 * np.sin(2 * np.pi * 3 * f * t) * np.exp(-t / 0.18)
            + 0.08 * np.sin(2 * np.pi * 7.02 * f * t) * np.exp(-t / 0.05))      # tine "bark"
    trem = 1 + 0.06 * np.sin(2 * np.pi * 4.2 * t)
    return vel * tone * trem * env(n, 0.006, 1.1)


def bell(note: int, dur: float, vel: float = 1.0) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = hz(note)
    tone = (np.sin(2 * np.pi * f * t) + 0.35 * np.sin(2 * np.pi * 2.76 * f * t) * np.exp(-t / 0.4)
            + 0.15 * np.sin(2 * np.pi * 5.4 * f * t) * np.exp(-t / 0.15))
    return vel * tone * env(n, 0.003, 0.9)


def bass(note: int, dur: float) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = hz(note)
    tone = np.sin(2 * np.pi * f * t) + 0.25 * np.sin(2 * np.pi * 2 * f * t)
    return tone * env(n, 0.02, 0.9)


def shaker(dur: float, vel: float) -> np.ndarray:
    n = int(dur * SR)
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(n)
    noise = np.diff(np.concatenate([[0], noise]))           # crude high-pass
    return vel * noise * env(n, 0.002, 0.03)


def kick(dur: float = 0.35) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = 110 * np.exp(-t / 0.05) + 45
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, 0.002, 0.12)


def add(buf: np.ndarray, sig: np.ndarray, at: float, gain: float = 1.0):
    i = int(at * SR)
    j = min(len(buf), i + len(sig))
    buf[i:j] += gain * sig[: j - i]


# F major: Fmaj7, Am7, Bbmaj7, C6/9 -> home on Fmaj9
CHORDS = {"F": [53, 57, 60, 64], "Am": [57, 60, 64, 67], "Bb": [58, 62, 65, 69],
          "C": [60, 64, 67, 69, 74], "F9": [53, 57, 60, 64, 67]}
ROOTS = {"F": 41, "Am": 45, "Bb": 46, "C": 48, "F9": 41}


def render(progression, motif, tail: float, fade_in=False) -> np.ndarray:
    length = sum(b for _, b in progression) * BEAT + tail
    buf = np.zeros(int(length * SR))
    at = 0.0
    for name, beats in progression:
        dur = beats * BEAT + (tail if name == "F9" else 0.4)
        for k, note in enumerate(CHORDS[name]):            # gentle strum
            add(buf, epiano(note, dur, 0.55), at + k * 0.012, 0.32)
        add(buf, bass(ROOTS[name], min(dur, beats * BEAT + 0.3)), at, 0.55)
        if name != "F9":
            for b in range(int(beats)):
                add(buf, kick(), at + b * BEAT, 0.35 if b % 2 == 0 else 0.0)
                for e in range(2):
                    add(buf, shaker(0.12, 0.05 if e else 0.03), at + (b + e / 2) * BEAT)
        at += beats * BEAT
    for beat_pos, note, beats in motif:                     # the melody
        add(buf, bell(note, beats * BEAT + 0.8), beat_pos * BEAT, 0.20)
    # soft room: a few short, quiet echoes
    for d, g in ((0.11, 0.18), (0.23, 0.10), (0.37, 0.05)):
        shift = int(d * SR)
        buf[shift:] += g * buf[:-shift].copy()
    if fade_in:
        n = int(0.4 * SR)
        buf[:n] *= np.linspace(0, 1, n)
    n = int(1.6 * SR)                                       # always fade out
    buf[-n:] *= np.linspace(1, 0, n) ** 2
    buf = np.tanh(buf * 1.2) / np.tanh(1.2)                 # gentle glue
    return buf / (np.max(np.abs(buf)) + 1e-9) * 0.62        # peak about -4 dBFS


def write_mp3(sig: np.ndarray, path: Path):
    pcm = (np.clip(sig, -1, 1) * 32767).astype(np.int16).tobytes()
    enc = lameenc.Encoder()
    enc.set_bit_rate(48)
    enc.set_in_sample_rate(SR)
    enc.set_channels(1)
    enc.set_quality(2)
    path.write_bytes(enc.encode(pcm) + enc.flush())
    print(f"  {path.name}: {len(sig) / SR:.1f}s, {path.stat().st_size / 1024:.0f} KB")


def main():
    OUT.mkdir(exist_ok=True)
    intro = render([("F", 2), ("Am", 2), ("Bb", 2), ("C", 2), ("F9", 1)],
                   motif=[(0, 72, 1), (1, 76, 0.5), (1.5, 77, 0.5), (2, 79, 1.5), (4, 77, 1), (5, 74, 1),
                          (6, 72, 0.5), (6.5, 74, 0.5), (7, 76, 1), (8, 77, 2)],
                   tail=2.2)
    outro = render([("Bb", 2), ("C", 2), ("Am", 2), ("Bb", 1), ("C", 1), ("F9", 1)],
                   motif=[(0, 77, 1), (1, 74, 1), (2, 76, 1), (3, 72, 1), (4, 76, 1), (5, 79, 1),
                          (6, 77, 1), (7, 76, 1), (8, 72, 3)],
                   tail=3.0, fade_in=True)
    write_mp3(intro, OUT / "theme_intro.mp3")
    write_mp3(outro, OUT / "theme_outro.mp3")


if __name__ == "__main__":
    main()
