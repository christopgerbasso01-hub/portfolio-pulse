"""Render one passage in several free voices (Kokoro, Piper high) for listening tests."""
import os, urllib.request, wave, io
import numpy as np, lameenc

TEXT = ("Welcome to Portfolio Pulse Weekly. I'm Alex, and I'm joined by Sam. "
        "This week your portfolio moved with the market, and we're going to unpack why a budget debate "
        "in London matters for a Canadian investor who holds American assets. "
        "Hang on, why would that happen? Right. Think of it like a seesaw: when the US dollar gets stronger, "
        "the same American shares are worth more once they're translated back into Canadian dollars.")
os.makedirs("samples", exist_ok=True)

def dl(url, dest):
    if not os.path.exists(dest):
        urllib.request.urlretrieve(url, dest)

def to_mp3(pcm16: bytes, rate: int, path: str):
    e = lameenc.Encoder(); e.set_bit_rate(64); e.set_in_sample_rate(rate); e.set_channels(1); e.set_quality(2)
    open(path, "wb").write(e.encode(pcm16) + e.flush())

# ── Kokoro ──
R = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
dl(f"{R}/kokoro-v1.0.int8.onnx", "kokoro.onnx"); dl(f"{R}/voices-v1.0.bin", "voices.bin")
from kokoro_onnx import Kokoro
k = Kokoro("kokoro.onnx", "voices.bin")
for v in ["am_michael", "am_adam", "am_fenrir", "af_heart", "af_bella", "af_nicole", "af_sarah"]:
    try:
        s, sr = k.create(TEXT, voice=v, speed=1.05, lang="en-us")
        to_mp3((np.clip(s, -1, 1) * 32767).astype(np.int16).tobytes(), sr, f"samples/kokoro_{v}.mp3")
        print("ok", v)
    except Exception as e:
        print("FAIL", v, e)

# ── Piper high-quality ──
from piper import PiperVoice
B = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US"
for who, name in [("ryan", "en_US-ryan-high"), ("lessac", "en_US-lessac-high"), ("libritts", "en_US-libritts-high")]:
    try:
        dl(f"{B}/{who}/high/{name}.onnx", f"{name}.onnx"); dl(f"{B}/{who}/high/{name}.onnx.json", f"{name}.onnx.json")
        v = PiperVoice.load(f"{name}.onnx")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w: v.synthesize_wav(TEXT, w)
        buf.seek(0)
        with wave.open(buf) as w: pcm, rate = w.readframes(w.getnframes()), w.getframerate()
        to_mp3(pcm, rate, f"samples/piper_{name}.mp3"); print("ok", name)
    except Exception as e:
        print("FAIL", name, e)
