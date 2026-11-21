"""Tests the Hugging Face rating logic with a fake client (no network)."""
import sys, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import core, huggingface_hub

calls = []
class FakeClient:
    def __init__(self, token=None): pass
    def chat_completion(self, model, messages, max_tokens, temperature):
        calls.append(model)
        if model.startswith("Qwen"):  # first default model "unavailable" -> must fall through
            raise RuntimeError("model not supported by provider")
        n = messages[1]["content"].count("\n[")  # number of excerpts in this batch
        rows = ",".join('{"id":%d,"hook":90,"emotion":80,"value":70,"share":85,"title":"AI title %d","reason":"strong opening"}' % (i, i) for i in range(n + 1) if f"[{i}]" in messages[1]["content"])
        msg = types.SimpleNamespace(content="Sure! Here you go:\n```json\n[" + rows + "]\n```")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])
huggingface_hub.InferenceClient = FakeClient

words = [{"w": w, "s": i * 0.4, "e": i * 0.4 + 0.3} for i, w in enumerate(("Why does nobody talk about this? " * 400).split())]
clips, notes = core.find_clips(words, 400.0, [0.1] * 400, {"num_clips": 3, "min_len": 20, "max_len": 40, "hf_token": "hf_x", "llm_model": ""}, lambda *a: None)
print(notes, calls[:3]); print([(c["title"], c["score"]) for c in clips])
assert "AI scoring used" in notes[0] and calls[0].startswith("Qwen") and calls[1].startswith("meta-llama")
assert all(c["title"].startswith("AI title") for c in clips) and len(clips) == 3

# token present but LLM fully failing -> falls back to built-in scoring, no crash
class Broken(FakeClient):
    def chat_completion(self, **k): raise RuntimeError("401 invalid token")
huggingface_hub.InferenceClient = Broken
clips, notes = core.find_clips(words, 400.0, [0.1] * 400, {"num_clips": 3, "min_len": 20, "max_len": 40, "hf_token": "bad"}, lambda *a: None)
print(notes); assert "skipped" in notes[0] and len(clips) == 3
print("LLM MOCK TESTS PASSED")
