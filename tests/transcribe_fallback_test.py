"""The old-PyAV bug: open() got an unexpected keyword argument 'metadata_errors'. Uses a fake Whisper model."""
import subprocess, sys, tempfile, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import numpy as np, core, faster_whisper

wav = Path(tempfile.mkdtemp()) / "a.wav"
subprocess.run([core.ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=300:d=3", "-ar", "16000", "-ac", "1", str(wav)], check=True)
seen = {}
class FakeModel:
    def __init__(self, *a, **k): pass
    def transcribe(self, audio, **kw):
        if isinstance(audio, str):
            raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")
        seen["len"] = len(audio); seen["dtype"] = audio.dtype
        w = types.SimpleNamespace(word=" hello", start=0.1, end=0.5)
        seg = types.SimpleNamespace(words=[w], end=0.5)
        return iter([seg]), types.SimpleNamespace(language="en")
faster_whisper.WhisperModel = FakeModel
out = core.transcribe(wav, "base", None, 3.0, lambda *a: None)
print(out, seen)
assert out["words"] == [{"w": "hello", "s": 0.1, "e": 0.5}] and seen["len"] == 48000 and seen["dtype"] == np.float32
print("TRANSCRIBE FALLBACK TEST PASSED")
