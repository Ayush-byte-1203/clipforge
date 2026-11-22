"""ClipForge core: download -> transcribe -> find viral moments -> render 9:16 clips with captions."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import wave
from pathlib import Path

QUALITIES = {  # name: (width, height, crf)
    "480p": (480, 854, 27),
    "720p": (720, 1280, 23),
    "1080p": (1080, 1920, 20),
}
DEFAULT_LLMS = [
    "Qwen/Qwen2.5-72B-Instruct",
    "meta-llama/Llama-3.3-70B-Instruct",
    "meta-llama/Llama-3.1-8B-Instruct",
]

# ----------------------------------------------------------------------------- helpers


def ensure_ffmpeg_in_path():
    venv_bin = Path(__file__).parent / ".venv" / "bin"
    if venv_bin.exists():
        ff_link = venv_bin / "ffmpeg"
        if not ff_link.exists():
            try:
                import imageio_ffmpeg
                ff_link.symlink_to(imageio_ffmpeg.get_ffmpeg_exe())
            except Exception:
                pass
        curr_path = os.environ.get("PATH", "")
        if str(venv_bin) not in curr_path:
            os.environ["PATH"] = f"{venv_bin}:{curr_path}"


ensure_ffmpeg_in_path()

DATA_DIR = Path(os.environ.get("CLIPFORGE_DATA", Path(__file__).parent / "data")).resolve()


def _apply_cookie_opts(target_opts: dict, cookies_file: str = "", cookies_browser: str = ""):
    """Ensure yt-dlp receives authenticated cookies to prevent 'Sign in to confirm you’re not a bot'."""
    if cookies_file and Path(cookies_file).expanduser().is_file():
        target_opts["cookiefile"] = str(Path(cookies_file).expanduser())
    elif (DATA_DIR / "cookies.txt").is_file():
        target_opts["cookiefile"] = str(DATA_DIR / "cookies.txt")
    elif cookies_browser:
        if cookies_browser.lower() != "safari" and sys.platform in ("darwin", "win32"):
            target_opts["cookiesfrombrowser"] = (cookies_browser,)
    elif sys.platform in ("darwin", "win32"):
        # Auto-fallback to Chrome cookies if present on Mac/Windows desktop
        try:
            target_opts["cookiesfrombrowser"] = ("chrome",)
        except Exception:
            pass


def ffmpeg_bin() -> str:
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise RuntimeError("ffmpeg was not found. Install it from https://ffmpeg.org and restart ClipForge.")


def run(cmd, cwd=None):
    cmd = [str(c) for c in cmd]
    if cmd and cmd[0] == ffmpeg_bin():
        cmd.insert(1, "-hide_banner")
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        lines = [ln for ln in r.stderr.splitlines() if ln.strip() and not ln.lstrip().startswith(("lib", "configuration", "built with"))]
        raise RuntimeError("ffmpeg failed: " + "\n".join(lines)[-700:])
    return r


def has_filter(name: str) -> bool:
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-filters"], capture_output=True, text=True, errors="replace")
    return any(re.search(rf"\s{name}\s", ln) for ln in r.stdout.splitlines())


def probe(path) -> dict:
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(path)], capture_output=True, text=True, errors="replace")
    t = r.stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", t)
    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0
    m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", t)
    w, h = (int(m[1]), int(m[2])) if m else (0, 0)
    if re.search(r"rotation of -?(90|270)\.", t):
        w, h = h, w
    if not w or not h or not dur:
        raise RuntimeError("Could not read this video file (is it a valid video?).")
    return {"duration": dur, "width": w, "height": h, "has_audio": "Audio:" in t}


def need_space(path, nbytes: int):
    Path(path).mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(path).free
    if free < nbytes:
        raise RuntimeError(
            f"Not enough free disk space: about {nbytes / 1e9:.1f} GB is needed but only {free / 1e9:.1f} GB is free. "
            "Free some space, lower 'Source quality' under Storage, or start ClipForge with the CLIPFORGE_DATA "
            "setting pointing to a bigger drive (for example an external disk).")


def slug(s: str, n=40) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:n] or "clip"


# ----------------------------------------------------------------------------- download


def is_url(s: str) -> bool:
    return bool(re.match(r"^https?://", s.strip(), re.I))


JS_RUNTIMES = {  # name -> extra places to look besides PATH
    "deno": [Path.home() / ".deno/bin/deno", "/opt/homebrew/bin/deno", "/usr/local/bin/deno"],
    "node": ["/opt/homebrew/bin/node", "/usr/local/bin/node"],
    "bun": [Path.home() / ".bun/bin/bun", "/opt/homebrew/bin/bun"],
    "quickjs": ["/opt/homebrew/bin/qjs", "/usr/local/bin/qjs"],
}
# YouTube "player clients" to try, in order, when the default one hands back no downloadable formats
CLIENT_ATTEMPTS = [None, ["android"], ["tv"], ["mweb", "android"], ["web_safari"], ["android_vr"]]


def find_js_runtimes() -> dict:
    """YouTube needs a JavaScript runtime so yt-dlp can unlock video formats. Find any installed one."""
    found = {}
    for name, extra in JS_RUNTIMES.items():
        path = shutil.which(name) or next((str(x) for x in extra if Path(x).exists()), None)
        if path:
            found[name] = {"path": path}
    return found


def download(url: str, out_dir: Path, live_minutes: int, report, max_height: int = 1080,
             cookies_browser: str = "", cookies_file: str = "") -> tuple[Path, str]:
    """Download a video (or record a live stream for `live_minutes`). Returns (file, title)."""
    import yt_dlp

    out_dir.mkdir(parents=True, exist_ok=True)
    base = {"quiet": True, "no_warnings": True, "noplaylist": True, "no_color": True, "retries": 5,
            "remote_components": ["ejs:github"]}
    js = find_js_runtimes()
    if js:
        base["js_runtimes"] = js
    _apply_cookie_opts(base, cookies_file, cookies_browser)
    if not shutil.which("ffmpeg"):
        base["ffmpeg_location"] = ffmpeg_bin()

    vfmt = f"bv*[height<={max_height}]+ba/b[height<={max_height}]/bv*+ba/b/best[height<={max_height}]/best"
    info, good, last = None, None, None
    for clients in CLIENT_ATTEMPTS:
        o = {**base, "format": vfmt}
        if clients:
            o["extractor_args"] = {"youtube": {"player_client": clients}}
        try:
            with yt_dlp.YoutubeDL(o) as y:
                info = y.extract_info(url, download=False)  # also selects the format, so "no formats" fails here
            good = o
            break
        except (ValueError, yt_dlp.utils.DownloadError, Exception) as e:
            err_str = str(e).lower()
            if ("unsupported platform" in err_str or "operation not permitted" in err_str or "permission" in err_str or "cookies" in err_str) and ("cookiesfrombrowser" in o or "cookiesfrombrowser" in base):
                print(f"[clipforge] Browser cookies access failed ({e}), continuing without cookies...")
                o.pop("cookiesfrombrowser", None)
                base.pop("cookiesfrombrowser", None)
                try:
                    with yt_dlp.YoutubeDL(o) as y:
                        info = y.extract_info(url, download=False)
                    good = o
                    break
                except Exception as e2:
                    last = e2
                    continue
            last = e
            if re.search(r"format is not available|only images|no video formats|sign in|confirm you|bot|forbidden", str(e), re.I):
                report("YouTube restricted format, trying alternate player client...")
                continue
            if "operation not permitted" in err_str and "cookies" in err_str:
                base.pop("cookiesfrombrowser", None)
                o.pop("cookiesfrombrowser", None)
                continue
            raise
    if info is None:
        raise last
    title = info.get("title") or "video"
    bps = {480: 300_000, 720: 600_000, 1080: 1_000_000}.get(max_height, 1_000_000)  # rough bytes/second
    est = info.get("filesize_approx") or (live_minutes * 60 if info.get("is_live") else (info.get("duration") or 0)) * bps
    need_space(out_dir, int(est * 1.3) + 700_000_000)

    if info.get("is_live"):
        # Record the live stream for N minutes, then clip what was recorded.
        report(f"Live stream detected, recording {live_minutes} min...")
        with yt_dlp.YoutubeDL({**good, "format": f"best[height<={max_height}]/best"}) as y:
            li = y.extract_info(url, download=False)
        stream = li.get("url") or (li.get("requested_formats") or [{}])[0].get("url")
        if not stream:
            raise RuntimeError("Could not resolve the live stream URL.")
        dst = out_dir / "source.mp4"
        hdrs = "".join(f"{k}: {v}\r\n" for k, v in (li.get("http_headers") or {}).items())
        run([ffmpeg_bin(), "-y", "-headers", hdrs, "-i", stream, "-t", live_minutes * 60, "-c", "copy",
             "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", dst])
        return dst, title

    def hook(d):
        if d.get("status") == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if tot:
                report(f"Downloading video... {d.get('downloaded_bytes', 0) * 100 // tot}%", d["downloaded_bytes"] / tot)

    opts = {**good, "outtmpl": str(out_dir / "source.%(ext)s"), "merge_output_format": "mp4", "progress_hooks": [hook]}
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([url])
    except Exception as e:
        if ("operation not permitted" in str(e).lower() or "permission" in str(e).lower()) and "cookiesfrombrowser" in opts:
            opts.pop("cookiesfrombrowser", None)
            with yt_dlp.YoutubeDL(opts) as y:
                y.download([url])
        else:
            raise
    files = sorted(out_dir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    files = [f for f in files if f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]
    if not files:
        raise RuntimeError("Download finished but no video file was found.")
    return files[0], title


def health() -> dict:
    """Checks the things that commonly break, so problems are shown up front instead of mid-job."""
    import importlib.util

    warns, det = [], {}
    try:
        det["ffmpeg"] = ffmpeg_bin()
        if not has_filter("ass"):
            warns.append("Your ffmpeg cannot draw captions (no libass). Install a full ffmpeg build, e.g. 'brew install ffmpeg'.")
    except Exception as e:
        warns.append(str(e))
    try:
        import yt_dlp.version as yv

        det["yt-dlp"] = yv.__version__
    except Exception:
        warns.append("yt-dlp is not installed. Run: pip install -U 'yt-dlp[default]'")
    js = find_js_runtimes()
    det["js_runtime"] = ", ".join(js) or "none"
    if not js:
        warns.append("YouTube downloads need a small helper called Deno, and it was not found. In Terminal run: "
                     "curl -fsSL https://deno.land/install.sh | sh   then close Terminal, open a new one and start ClipForge again. "
                     "(Ignore this if you only upload files.)")
    det["yt-dlp-ejs"] = bool(importlib.util.find_spec("yt_dlp_ejs"))
    for mod in ("faster_whisper", "cv2", "numpy"):
        if not importlib.util.find_spec(mod):
            warns.append(f"Python package '{mod}' is missing. Run: pip install -r requirements.txt")
    try:
        from importlib.metadata import version

        det["av"] = version("av")
        if int(det["av"].split(".")[0]) < 14:
            warns.append(f"Audio library 'av' {det['av']} is old. Run: pip install -U av")
    except Exception:
        pass
    return {"ok": not warns, "warnings": warns, "details": det}


# ----------------------------------------------------------------------------- transcription


def extract_audio(src: Path, wav: Path):
    run([ffmpeg_bin(), "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav])


def load_wav_float32(wav: Path):
    import numpy as np

    with wave.open(str(wav), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def clean_transcript_word(w: str) -> str:
    """Clean hallucinated Arabic/Persian symbols, bad punctuation merges, and artifacts."""
    w = re.sub(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF\u0000-\u001F]+", "", w)
    w = re.sub(r"(\w+)'re\b", r"\1 are", w)
    w = re.sub(r"[śźżłćń]+", "", w)
    return w.strip()


def transcribe(wav: Path, model_size: str, language: str | None, duration: float, report) -> dict:
    from faster_whisper import WhisperModel

    device, ctype = "cpu", "int8"
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            device, ctype = "cuda", "float16"
    except Exception:
        pass
    report(f"Loading speech model '{model_size}' ({device})...")
    threads = max(4, os.cpu_count() or 4)
    model = WhisperModel(model_size, device=device, compute_type=ctype, cpu_threads=threads)

    task = "transcribe"
    clean_lang = (language or "").strip().lower()
    if clean_lang == "hi_en":
        # Indian gaming / Hinglish: Translate Hindi audio to clean English subtitles
        clean_lang = "hi"
        task = "translate"
    elif clean_lang in ("translate", "en_translate"):
        clean_lang = None
        task = "translate"
    elif not clean_lang:
        clean_lang = None

    kw = dict(
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=400),
        beam_size=5,
        best_of=5,
        temperature=[0.0, 0.2],
        condition_on_previous_text=False,
        language=clean_lang,
        task=task,
        initial_prompt="Clean dialogue with proper punctuation, grammar, and spelling.",
    )
    try:
        segs, info = model.transcribe(str(wav), **kw)
    except TypeError as e:  # old PyAV on this machine can't open the file: decode the wav ourselves instead
        if "metadata_errors" not in str(e):
            raise
        report("Using built-in audio loader...")
        segs, info = model.transcribe(load_wav_float32(wav), **kw)
    words = []
    for s in segs:  # generator: streams, so videos of any length work
        for w in s.words or []:
            t = clean_transcript_word(w.word)
            if t:
                words.append({"w": t, "s": round(float(w.start), 2), "e": round(float(w.end), 2)})
        report(f"Transcribing... {int(100 * min(1, s.end / max(duration, 1)))}%", min(1, s.end / max(duration, 1)))
    return {"language": info.language, "words": words}


def energy_per_second(wav: Path) -> list[float]:
    """Loudness (RMS) for each second of audio, read in chunks so any length works."""
    import numpy as np

    out = []
    with wave.open(str(wav), "rb") as wf:
        sr = wf.getframerate()
        while True:
            raw = wf.readframes(sr * 60)
            if not raw:
                break
            a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            n = len(a) // sr
            if n:
                out.extend(np.sqrt((a[: n * sr].reshape(n, sr) ** 2).mean(axis=1)).tolist())
            if len(a) % sr:
                out.append(float(np.sqrt((a[n * sr:] ** 2).mean())))
    return out


# ----------------------------------------------------------------------------- finding viral moments

HOOK_WORDS = set("""never secret secrets stop why how nobody everyone biggest worst best mistake mistakes truth lie lies
crazy insane shocking unbelievable actually literally wrong problem warning don't dont won't wont fail failed failure
million billion free easy simple hack trick reason reasons important need must changed changes everything nothing
first last only real fake hidden exposed proof""".split())
EMOTION_WORDS = set("""love hate fear afraid angry amazing incredible terrible awful horrible beautiful hilarious funny
wow oh god damn shit fuck crazy insane wild nuts huge massive brutal painful pain scared excited obsessed dead die
killed win won lost cry cried laugh laughed shocked surprised unreal epic legend""".split())
FILLER_START = ("um", "uh", "so yeah", "yeah so", "okay so", "and yeah")


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def build_sentences(words: list[dict]) -> list[dict]:
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if re.search(r"[.!?…。！？]$", w["w"]) or nxt is None or nxt["s"] - w["e"] > 0.9 or len(cur) >= 30:
            out.append({"s": cur[0]["s"], "e": cur[-1]["e"], "text": " ".join(x["w"] for x in cur), "n": len(cur)})
            cur = []
    return out


def candidates(sents: list[dict], min_len: float, max_len: float) -> list[tuple[int, int]]:
    out, n = [], len(sents)
    targets = [min_len, (min_len + max_len) / 2, max_len]
    for i in range(n):
        seen = set()
        for tgt in targets:
            j = i
            while j < n - 1 and sents[j]["e"] - sents[i]["s"] < tgt:
                j += 1
            dur = sents[j]["e"] - sents[i]["s"]
            if j in seen or dur < min_len * 0.7 or dur > max_len * 1.15:
                continue
            seen.add(j)
            out.append((i, j))
    return out


def heuristic_score(sents, i, j, energy, gmean, gstd) -> tuple[float, dict]:
    seg = sents[i : j + 1]
    text = " ".join(s["text"] for s in seg)
    dur = seg[-1]["e"] - seg[0]["s"]
    first = seg[0]["text"]
    toks = re.findall(r"[\w']+", text.lower())
    ftoks = re.findall(r"[\w']+", first.lower())
    nw = max(len(toks), 1)

    hook = 0.0
    hook += 12 if first.strip().endswith("?") else 0
    hook += 10 if any(t in HOOK_WORDS for t in ftoks) else 0
    hook += 4 if re.search(r"\d", first) else 0
    hook += 4 if len(ftoks) <= 18 else 0
    hook = min(hook, 30) / 30
    if first.lower().startswith(FILLER_START):
        hook *= 0.5

    emo_hits = sum(t in EMOTION_WORDS for t in toks) + text.count("!") * 2 + text.count("?")
    emotion = clamp(emo_hits / max(4, nw / 12))
    wps = nw / dur
    pace = clamp(1 - abs(wps - 2.8) / 2.0)
    density = clamp((len(set(toks)) / nw - 0.35) / 0.4)

    a, b = int(seg[0]["s"]), int(seg[-1]["e"]) + 1
    sl = energy[a:b] if energy else []
    if sl and gmean > 0:
        mean_ratio = (sum(sl) / len(sl)) / gmean
        mx = max(sl)
        var = (sum((x - sum(sl) / len(sl)) ** 2 for x in sl) / len(sl)) ** 0.5
        eng = clamp((mean_ratio - 0.8) / 0.9) * 0.6 + clamp(var / (gstd * 1.2 + 1e-9)) * 0.25 + clamp((mx / gmean - 1.5) / 3) * 0.15
    else:
        eng = 0.3
    sparse = 0.5 if wps < 1.2 else 1.0
    comp = {"hook": hook, "emotion": emotion, "pace": pace, "energy": eng, "density": density}
    total = (0.30 * hook + 0.20 * emotion + 0.15 * pace + 0.25 * eng + 0.10 * density) * sparse
    return round(100 * total, 1), {k: round(100 * v) for k, v in comp.items()}


def overlap(a, b):
    inter = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    return inter / max(1e-9, min(a["end"] - a["start"], b["end"] - b["start"]))


def pick_non_overlapping(items: list[dict], k: int, max_ov=0.2) -> list[dict]:
    out = []
    for it in sorted(items, key=lambda x: x["score"], reverse=True):
        if all(overlap(it, o) <= max_ov for o in out):
            out.append(it)
            if len(out) >= k:
                break
    return out


def llm_rank(pool: list[dict], token: str, model: str | None, report) -> tuple[bool, str]:
    """Rate each candidate with a Hugging Face hosted LLM. Mutates pool items. Returns (ok, message)."""
    from huggingface_hub import InferenceClient

    client = InferenceClient(token=token)
    models = [model] if model else DEFAULT_LLMS
    system = ("You are an expert short-form video editor for TikTok, Reels and YouTube Shorts. You rate transcript "
              "excerpts for viral potential. Reply with ONLY a JSON array, no other text.")
    batch_n, active, last_err = 6, None, ""
    for b in range(0, len(pool), batch_n):
        batch = pool[b : b + batch_n]
        body = "\n\n".join(f"[{k}]\n{c['text'][:1100]}" for k, c in enumerate(batch))
        user = ("Rate each excerpt from 0-100 on: hook (does the first line grab attention), emotion, value "
                "(insight/entertainment) and share (would people send it to a friend). Also write a punchy title of at "
                "most 8 words and a reason of at most 14 words.\nFormat: "
                '[{"id":0,"hook":80,"emotion":60,"value":70,"share":65,"title":"...","reason":"..."}]\n\n' + body)
        data = None
        for m in ([active] if active else models):
            try:
                r = client.chat_completion(model=m, messages=[{"role": "system", "content": system},
                                                              {"role": "user", "content": user}],
                                           max_tokens=900, temperature=0.2)
                txt = r.choices[0].message.content
                mm = re.search(r"\[.*\]", txt, re.S)
                data = json.loads(mm.group(0))
                active = m
                break
            except Exception as e:  # try next model
                last_err = f"{m}: {str(e)[:160]}"
        if data is None:
            return False, last_err or "LLM returned nothing"
        for row in data:
            try:
                c = batch[int(row["id"])]
                c["llm"] = {k: clamp(float(row[k]), 0, 100) for k in ("hook", "emotion", "value", "share")}
                c["title"] = str(row.get("title") or "")[:90]
                c["reason"] = str(row.get("reason") or "")[:140]
            except Exception:
                continue
        report(f"AI is rating clips... {min(b + batch_n, len(pool))}/{len(pool)}", None)
    return True, active or ""


def find_clips(words, duration, energy, opts, report) -> tuple[list[dict], list[str]]:
    notes = []
    k = int(opts["num_clips"])
    min_len, max_len = float(opts["min_len"]), float(opts["max_len"])
    if duration <= min_len:  # very short source: one clip of the whole thing
        txt = " ".join(w["w"] for w in words)
        return [{"start": 0.0, "end": min(duration, max_len), "score": 50.0, "text": txt,
                 "title": (txt[:60] or "Full video"), "reason": "Source is shorter than the minimum clip length", "parts": {}}], notes
    sents = build_sentences(words)
    if duration > 0:
        sents = [s for s in sents if s["s"] < duration]
    if len(words) < 8 or not sents:  # no speech or empty: fall back to loudness-only windows
        notes.append("No speech detected, ranking by audio energy only.")
        sents = [{"s": t, "e": min(duration, t + 8.0), "text": "", "n": 0} for t in range(0, max(1, int(duration) - 7), 8)]
    flat = [x for x in energy if x > 0] or [0.0]
    gmean = sum(flat) / len(flat)
    gstd = (sum((x - gmean) ** 2 for x in flat) / len(flat)) ** 0.5
    items = []
    for i, j in candidates(sents, min_len, max_len):
        c_start = max(0.0, sents[i]["s"] - 0.15)
        c_end = min(duration, sents[j]["e"] + 0.35)
        if c_end <= c_start or (c_end - c_start) < min_len * 0.7:
            continue
        sc, parts = heuristic_score(sents, i, j, energy, gmean, gstd)
        items.append({"start": c_start, "end": c_end,
                      "score": sc, "heur": sc, "parts": parts, "text": " ".join(s["text"] for s in sents[i : j + 1]),
                      "first": sents[i]["text"]})
    if not items:  # e.g. one endless sentence
        safe_end = min(duration, max_len)
        items = [{"start": 0.0, "end": max(min_len, safe_end), "score": 40.0, "heur": 40.0, "parts": {},
                  "text": " ".join(w["w"] for w in words), "first": ""}]
    pool = pick_non_overlapping(items, max(k * 3, 12), max_ov=0.3)
    token = (opts.get("hf_token") or os.environ.get("HF_TOKEN") or "").strip()
    if token and any(c["text"] for c in pool):
        report("AI is rating the best moments...", None)
        try:
            ok, msg = llm_rank(pool, token, (opts.get("llm_model") or "").strip() or None, report)
        except Exception as e:
            ok, msg = False, str(e)[:200]
        if ok:
            for c in pool:
                if "llm" in c:
                    avg = sum(c["llm"].values()) / 4
                    c["score"] = round(0.65 * avg + 0.35 * c["heur"], 1)
                    c["parts"] = {**c["parts"], **{f"ai_{a}": round(b) for a, b in c["llm"].items()}}
                else:
                    c["score"] = round(c["heur"] * 0.8, 1)
            notes.append(f"AI scoring used: {msg}")
        else:
            notes.append(f"AI scoring skipped ({msg}). Used built-in scoring instead.")
    else:
        notes.append("Used built-in scoring (no Hugging Face token given).")
    best = pick_non_overlapping(pool, k)
    for c in best:
        if not c.get("title"):
            ft = re.sub(r"\s+", " ", c.get("first", "")).strip().rstrip(".,;:")
            c["title"] = " ".join(ft.split()[:9]) or "Highlight"
        c.setdefault("reason", "")
    best.sort(key=lambda c: c["score"], reverse=True)
    return best, notes


# ----------------------------------------------------------------------------- 9:16 face-centered crop


def load_cascade():
    import cv2

    cands = [Path(__file__).parent / "assets" / "haarcascade_frontalface_default.xml"]
    if hasattr(cv2, "data"):
        cands.append(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
    for p in cands:
        if p.exists():
            cas = cv2.CascadeClassifier(str(p))
            if not cas.empty():
                return cas
    raise RuntimeError("face detector file missing (assets/haarcascade_frontalface_default.xml)")


def face_keys(src: Path, start: float, dur: float, W: int, H: int, cw: int) -> list[list[float]]:
    """Return [[t, x], ...] crop-x keyframes that keep the main speaker's face centred."""
    import cv2

    SW, SH = 640, 360
    s = min(SW / W, SH / H)
    iw, ox = W * s, (SW - W * s) / 2
    vf = (f"fps=3,scale={SW}:{SH}:force_original_aspect_ratio=decrease,"
          f"pad={SW}:{SH}:(ow-iw)/2:(oh-ih)/2,format=gray")
    p = subprocess.Popen([ffmpeg_bin(), "-v", "error", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", str(src),
                          "-an", "-vf", vf, "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    cas = load_cascade()
    import numpy as np

    samples, prev, i = [], None, 0
    while True:
        buf = p.stdout.read(SW * SH)
        if len(buf) < SW * SH:
            break
        g = np.frombuffer(buf, dtype=np.uint8).reshape(SH, SW)
        faces = cas.detectMultiScale(g, 1.15, 5, minSize=(28, 28))
        cx = None
        if len(faces):
            big = max(faces, key=lambda f: f[2] * f[3])
            pick = big
            if prev is not None:  # stay with the same person unless another is clearly bigger
                near = min(faces, key=lambda f: abs((f[0] + f[2] / 2) - prev))
                if near[2] * near[3] >= 0.5 * big[2] * big[3]:
                    pick = near
            c = pick[0] + pick[2] / 2
            prev = c
            cx = (c - ox) / iw * W
        samples.append((i / 3.0, cx))
        i += 1
    p.stdout.close()
    p.wait()
    maxx = max(0, W - cw)
    xs = [x for _, x in samples if x is not None]
    if not xs:
        return [[0.0, maxx / 2]]
    last = next(x for x in xs)
    filled = []
    for _, x in samples:  # carry the last known face position through gaps
        last = x if x is not None else last
        filled.append(last)
    pts = [min(max(x - cw / 2, 0), maxx) for x in filled]
    sm = []
    for k in range(len(pts)):  # median of 5 rejects false detections
        win = sorted(pts[max(0, k - 2) : k + 3])
        sm.append(win[len(win) // 2])
    cur, keys, last_t = sm[0], [[0.0, sm[0]]], 0.0
    for (t, _), x in zip(samples, sm):  # hold still until the speaker really moves
        if abs(x - cur) > 0.12 * cw and t - last_t >= 1.2 and len(keys) < 80:
            cur, last_t = x, t
            keys.append([round(t, 2), x])
    return keys


def x_expr(keys, tr=0.4) -> str:
    expr = f"{keys[-1][1]:.1f}"
    for k in range(len(keys) - 1, 0, -1):
        t1, x0, x1 = keys[k][0], keys[k - 1][1], keys[k][1]
        expr = (f"if(lt(t,{t1:.2f}),{x0:.1f},if(lt(t,{t1 + tr:.2f}),{x0:.1f}+({x1:.1f}-{x0:.1f})*(t-{t1:.2f})/{tr},{expr}))")
    return expr


# ----------------------------------------------------------------------------- captions (ASS)


def _t(sec: float) -> str:
    sec = max(0.0, sec)
    cs = int(round(sec * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _clean(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def make_ass(words: list[dict], dur: float, title: str, opts: dict) -> str:
    hi = {"gold": "&H00D7FF&", "green": "&H00F827&", "pink": "&HB469FF&", "cyan": "&HFFFF00&", "white": "&HFFFFFF&"}.get(
        opts.get("caption_color", "gold"), "&H00D7FF&")
    up = bool(opts.get("uppercase", True))
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
            "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\n"
            "Style: Cap,Poppins,64,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,2,2,60,60,220,1\n"
            "Style: Hook,Poppins,58,&H00000000,&H000000FF,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,16,0,8,80,80,190,1\n"
            "Style: CTA,Poppins,56,&H00FFFFFF,&H000000FF,&H00000000,&HCC000000,-1,0,0,0,100,100,0,0,3,14,0,8,60,60,140,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    ev = []
    if opts.get("hook", True) and title:
        lines, cur = [], ""
        for w in _clean(title).split():
            if len(cur) + len(w) + 1 > 22 and cur:
                lines.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        lines.append(cur)
        nl = "\\N"
        ev.append(f"Dialogue: 1,{_t(0)},{_t(min(3.5, dur))},Hook,,0,0,0,,{nl.join(lines[:3])}")
    if opts.get("captions", True) and words:
        groups, cur = [], []
        for k, w in enumerate(words):
            cur.append(w)
            nxt = words[k + 1] if k + 1 < len(words) else None
            chars = sum(len(x["w"]) + 1 for x in cur)
            if (nxt is None or len(cur) >= 4 or chars >= 20 or re.search(r"[.!?,;:]$", w["w"]) or nxt["s"] - w["e"] > 0.6):
                groups.append(cur)
                cur = []
        for gi, g in enumerate(groups):
            gend = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else g[-1]["e"] + 0.3
            gend = min(gend, g[-1]["e"] + 0.4)
            for k, w in enumerate(g):
                t0 = w["s"]
                t1 = g[k + 1]["s"] if k + 1 < len(g) else max(gend, w["e"])
                if t1 <= t0:
                    t1 = t0 + 0.08
                parts = []
                for m, x in enumerate(g):
                    tx = _clean(x["w"].upper() if up else x["w"])
                    parts.append(f"{{\\1c{hi}\\fscx112\\fscy112}}{tx}{{\\r}}" if m == k else tx)
                ev.append(f"Dialogue: 0,{_t(t0)},{_t(t1)},Cap,,0,0,0,,{' '.join(parts)}")
    if opts.get("cta", True) and dur >= 5.0:
        cta_start = max(0.0, dur - 4.5)
        cta_text = "{\\fad(400,200)\\b1\\c&H00D7FF&}👍 LIKE {\\c&HFFFFFF&}• {\\c&H00FFFF&}🔄 SHARE {\\c&HFFFFFF&}• {\\c&H0000FF&}🔔 SUBSCRIBE!"
        ev.append(f"Dialogue: 2,{_t(cta_start)},{_t(dur)},CTA,,0,0,0,,{cta_text}")
    return head + "\n".join(ev) + "\n"


# ----------------------------------------------------------------------------- render


def render_clip(src: Path, clip: dict, words: list[dict], vinfo: dict, quality: str, out: Path, opts: dict, work: Path, offset_words: float = 0.0):
    tw, th, crf = QUALITIES[quality]
    W, H = vinfo["width"], vinfo["height"]
    start, end = clip["start"], clip["end"]
    dur = end - start
    work.mkdir(parents=True, exist_ok=True)
    t_ref = offset_words if offset_words > 0 else start
    cw_ = [{"w": w["w"], "s": round(w["s"] - t_ref, 2), "e": round(w["e"] - t_ref, 2)}
           for w in words if w["s"] >= t_ref - 0.05 and w["e"] <= (t_ref + dur) + 0.05]

    assets_dir = Path(__file__).parent / "assets"
    gs_overlay = None
    for cand_name in ("like share subscribe.mp4", "subscribe.mp4", "subscribe_greenscreen.mp4"):
        if (assets_dir / cand_name).exists():
            gs_overlay = assets_dir / cand_name
            break
    if not gs_overlay:
        for p in assets_dir.glob("*.mp4"):
            if any(k in p.name.lower() for k in ("subscribe", "greenscreen", "like")):
                gs_overlay = p
                break

    gs_color = "0x00D605" if (gs_overlay and "like" in gs_overlay.name.lower()) else "0x00FF00"
    use_gs = bool(opts.get("cta", True) and gs_overlay and gs_overlay.exists() and dur >= 5.0)
    opts_ass = dict(opts)
    if use_gs:
        opts_ass["cta"] = False

    use_caps = bool(opts.get("captions", True) and cw_) or bool(opts.get("hook", True)) or bool(opts_ass.get("cta", False))
    if use_caps:
        (work / "cap.ass").write_text(make_ass(cw_, dur, clip.get("title", ""), opts_ass), encoding="utf-8")
        (work / "fonts").mkdir(exist_ok=True)
        for f in (Path(__file__).parent / "assets" / "fonts").glob("*.ttf"):
            shutil.copyfile(f, work / "fonts" / f.name)
    layout = opts.get("layout", "fill")
    if W / H <= 9 / 16 + 0.01:
        layout = "native"
    if layout == "fill":
        cw = int(H * 9 / 16) // 2 * 2
        kf = work / "crop.json"
        if kf.exists():
            keys = json.loads(kf.read_text())
        else:
            try:
                keys = face_keys(src, start, dur, W, H, cw)
            except Exception as e:
                print(f"[clipforge] face tracking unavailable, using centre crop: {e}")
                keys = [[0.0, (W - cw) / 2]]
            kf.write_text(json.dumps(keys))
        base = f"[0:v]crop={cw}:{H}:x='{x_expr(keys)}':y=0,scale={tw}:{th}:flags=lanczos,setsar=1[vb]"
    elif layout == "fit":  # whole frame centred over a blurred copy (ultra-fast downscaled blur)
        base = (f"[0:v]split=2[a][b];[a]scale=180:320:force_original_aspect_ratio=increase,crop=180:320,"
                f"boxblur=4:1,scale={tw}:{th}:flags=bilinear[bg];[b]scale={tw}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[vb]")
    elif layout == "split":  # Gaming Split: Top = Webcam (face-tracked), Bottom = Gameplay
        top_h = int(th * 0.45) // 2 * 2
        bot_h = th - top_h
        cw_face = int(H * (tw / top_h)) // 2 * 2
        cw_face = min(W, max(int(W * 0.4), cw_face))
        kf = work / "crop_face.json"
        if kf.exists():
            keys = json.loads(kf.read_text())
        else:
            try:
                keys = face_keys(src, start, dur, W, H, cw_face)
            except Exception:
                keys = [[0.0, (W - cw_face) / 2]]
            kf.write_text(json.dumps(keys))
        cw_game = int(H * (tw / bot_h)) // 2 * 2
        cw_game = min(W, cw_game)
        base = (f"[0:v]split=2[t_in][b_in];"
                f"[t_in]crop={cw_face}:{H}:x='{x_expr(keys)}':y=0,scale={tw}:{top_h}:flags=lanczos,setsar=1[top];"
                f"[b_in]crop={cw_game}:{H}:(in_w-{cw_game})/2:0,scale={tw}:{bot_h}:flags=lanczos,setsar=1[bot];"
                f"[top][bot]vstack=inputs=2,setsar=1[vb]")
    else:  # already portrait
        ch = int(W * 16 / 9) // 2 * 2
        base = f"[0:v]crop={W}:{min(ch, H)}:0:(ih-{min(ch, H)})/2,scale={tw}:{th},setsar=1[vb]"

    v_tagged = "[v_caps]" if use_gs else "[v]"
    graph = base + (f";[vb]ass=cap.ass:fontsdir=fonts{v_tagged}" if use_caps else f";[vb]null{v_tagged}")
    cmd = [ffmpeg_bin(), "-y", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", src]

    if use_gs:
        cta_start = max(0.0, dur - 6.5)
        cmd += ["-stream_loop", "-1", "-i", str(gs_overlay)]
        graph += (f";[1:v]colorkey={gs_color}:0.28:0.12,scale={tw}:-1[ck];"
                  f"[v_caps][ck]overlay=(W-w)/2:H-h-240:enable='gte(t,{cta_start:.2f})'[v]")
        if vinfo.get("has_audio"):
            graph += f";[0:a:0?][1:a]amix=inputs=2:duration=first[a_raw];[a_raw]loudnorm=I=-16:TP=-1.5:LRA=11[a]"
            cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
        else:
            cmd += ["-filter_complex", graph, "-map", "[v]"]
    else:
        cmd += ["-filter_complex", graph, "-map", "[v]"]
        if vinfo.get("has_audio"):
            cmd += ["-map", "0:a:0?", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "160k"]

    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", crf, "-pix_fmt", "yuv420p",
            "-t", f"{dur:.2f}", "-movflags", "+faststart", out.resolve()]
    run(cmd, cwd=work)
    return out


def extract_cloud_transcript(info: dict, ydl=None) -> dict | None:
    """Extract word-level or segment-level transcript from YouTube subtitles/auto-captions without downloading media."""
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    preferred_langs = ("en", "en-orig", "en-US", "en-GB", "hi", "es")
    candidates = []

    for s_dict in (subs, auto):
        for lang in preferred_langs:
            if lang in s_dict:
                candidates.extend(s_dict[lang])
        if not candidates:
            for lang, fmts in s_dict.items():
                if fmts:
                    candidates.extend(fmts)
                    break
        if candidates:
            break

    if not candidates:
        return None

    target = next((c for c in candidates if c.get("ext") == "json3"), None)
    if not target:
        target = next((c for c in candidates if c.get("ext") in ("vtt", "srv3")), candidates[0] if candidates else None)

    if not target or not target.get("url"):
        return None

    try:
        if ydl:
            resp = ydl.urlopen(target["url"])
            content = resp.read().decode("utf-8", errors="replace")
        else:
            import urllib.request
            req = urllib.request.Request(target["url"], headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                content = resp.read().decode("utf-8", errors="replace")

        words = []
        if target.get("ext") == "json3":
            data = json.loads(content)
            for ev in data.get("events", []):
                t_start = ev.get("tStartMs", 0) / 1000.0
                dur = ev.get("dDurationMs", 0) / 1000.0
                for s in ev.get("segs", []):
                    w_text = s.get("utf8", "").strip()
                    if not w_text or w_text == "\n":
                        continue
                    offset = s.get("tOffsetMs", 0) / 1000.0
                    w_s = t_start + offset
                    w_e = min(w_s + 0.35, t_start + dur)
                    words.append({"w": w_text, "s": round(w_s, 2), "e": round(w_e, 2)})
        else:
            lines = content.splitlines()
            time_re = re.compile(r"(\d{2}:\d{2}:\d{2}[\.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[\.,]\d{3})")
            for i, line in enumerate(lines):
                m = time_re.search(line)
                if m and i + 1 < len(lines):
                    s_str, e_str = m.groups()
                    def to_sec(t_str):
                        p = t_str.replace(",", ".").split(":")
                        return float(p[0]) * 3600 + float(p[1]) * 60 + float(p[2])
                    t0 = to_sec(s_str)
                    t1 = to_sec(e_str)
                    text_line = lines[i + 1].strip()
                    text_clean = re.sub(r"<[^>]+>", "", text_line)
                    w_list = text_clean.split()
                    if w_list:
                        dur_per_word = max(0.1, (t1 - t0) / len(w_list))
                        for w_i, word in enumerate(w_list):
                            w_s = t0 + w_i * dur_per_word
                            words.append({"w": word, "s": round(w_s, 2), "e": round(w_s + dur_per_word, 2)})

        if len(words) >= 5:
            return {"language": "en", "words": words}
    except Exception as e:
        print(f"[clipforge] Could not parse cloud transcript: {e}")
    return None


def download_clip_slice(
    url: str,
    start: float,
    duration: float,
    out_path: Path,
    max_height: int = 1080,
    cookies_browser: str = "",
    cookies_file: str = "",
) -> Path:
    """Download ONLY a short clip slice (HTTP Range request) instead of the whole video."""
    import yt_dlp

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pad_start = max(0.0, float(start) - 0.5)
    clean_dur = max(1.0, float(duration))
    pad_end = max(pad_start + 1.0, pad_start + clean_dur + 1.0)

    def section_filter(info_dict, ydl):
        return [{"start_time": pad_start, "end_time": pad_end}]

    vfmt = f"bv*[height<={max_height}]+ba/b[height<={max_height}]/best"
    tmpl = str(out_path.with_suffix(".%(ext)s"))
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": vfmt,
        "outtmpl": tmpl,
        "download_ranges": section_filter,
        "force_keyframes_at_cuts": True,
        "merge_output_format": "mp4",
        "remote_components": ["ejs:github"],
    }
    if not shutil.which("ffmpeg"):
        opts["ffmpeg_location"] = ffmpeg_bin()
    _apply_cookie_opts(opts, cookies_file, cookies_browser)

    with yt_dlp.YoutubeDL(opts) as y:
        y.download([url])

    if out_path.exists():
        return out_path
    for ext in (".mp4", ".mkv", ".webm", ".mov"):
        cand = out_path.with_suffix(ext)
        if cand.exists():
            if cand != out_path:
                cand.rename(out_path)
            return out_path
    raise RuntimeError(f"Could not download clip slice for range {start:.1f}s - {start + duration:.1f}s")


# ----------------------------------------------------------------------------- full pipeline


def run_pipeline(job_dir: Path, opts: dict, report, source: Path | None = None, url: str = "", title: str = "") -> dict:
    """report(message, fraction_in_stage or None, stage=None)."""
    if not has_filter("ass"):
        raise RuntimeError("Your ffmpeg build has no subtitle renderer (libass). Install a full ffmpeg build.")
    stage = lambda name, lo, hi: (lambda msg, f=None: report(msg, None if f is None else lo + (hi - lo) * f, name))
    if source is None:
        # ⚡ Fast Online Mode: check for instant YouTube cloud captions without downloading video
        if opts.get("fast_stream", True) and ("youtube.com" in url or "youtu.be" in url):
            report("⚡ Checking for instant online captions (zero-download mode)...", 0.05, "transcribe")
            import yt_dlp
            js = find_js_runtimes()
            base = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "skip_download": True,
                "remote_components": ["ejs:github"],
                "js_runtimes": js,
            }
            _apply_cookie_opts(base, opts.get("cookies_file", ""), opts.get("cookies_browser", ""))
            try:
                with yt_dlp.YoutubeDL(base) as y:
                    info = y.extract_info(url, download=False)
                    title = info.get("title") or "video"
                    dur_total = float(info.get("duration") or 0)
                    tr = extract_cloud_transcript(info, ydl=y)
                    if tr and len(tr.get("words", [])) >= 15:
                        vinfo = {
                            "width": info.get("width") or 1920,
                            "height": info.get("height") or 1080,
                            "duration": dur_total,
                            "has_audio": True,
                            "title": title,
                            "source": "online_stream",
                            "stream_url": url,
                            "is_stream_slice": True,
                        }
                        tj = job_dir / "transcript.json"
                        tj.write_text(json.dumps(tr), encoding="utf-8")
                        report("⚡ Found online captions! Finding viral clips in 2 seconds...", 0.3, "analyze")
                        clips, notes = find_clips(tr["words"], dur_total, [], opts, stage("analyze", 0.3, 0.75))
                        for n, c in enumerate(clips, 1):
                            c["n"] = n
                            c["duration"] = round(c["end"] - c["start"], 1)
                            c["start"], c["end"] = round(c["start"], 2), round(c["end"], 2)
                            c["text"] = c["text"][:1500]
                        return {
                            "video": vinfo,
                            "clips": clips,
                            "notes": notes + ["⚡ Fast Online Mode: Generated from cloud captions without full download!"],
                            "language": tr.get("language", "en"),
                            "is_stream_slice": True,
                        }
                    else:
                        report("⚡ Zero-download mode: Streaming audio track for instant transcription...", 0.1, "transcribe")
                        wav = job_dir / "audio.wav"
                        raw_audio = job_dir / "stream_audio"

                        target_sec = min(int(opts.get("live_minutes", 10)) * 60, dur_total) if dur_total > 1200 else dur_total
                        max_audio_bytes = int(target_sec * 20_000) if (target_sec and target_sec < dur_total) else None

                        class StopDownload(Exception):
                            pass

                        def chunk_hook(d):
                            if max_audio_bytes and d.get("status") == "downloading":
                                if d.get("downloaded_bytes", 0) >= max_audio_bytes:
                                    raise StopDownload()

                        js = find_js_runtimes()
                        audio_opts = {
                            "quiet": True,
                            "no_warnings": True,
                            "noplaylist": True,
                            "format": "ba/b[height<=480]",
                            "outtmpl": str(raw_audio) + ".%(ext)s",
                            "progress_hooks": [chunk_hook],
                            "remote_components": ["ejs:github"],
                            "js_runtimes": js,
                        }
                        _apply_cookie_opts(audio_opts, opts.get("cookies_file", ""), opts.get("cookies_browser", ""))

                        try:
                            with yt_dlp.YoutubeDL(audio_opts) as ay:
                                ay.download([url])
                        except (StopDownload, Exception) as e:
                            pass

                        audio_files = list(job_dir.glob("stream_audio.*"))
                        if audio_files:
                            extract_audio(audio_files[0], wav)
                            audio_files[0].unlink(missing_ok=True)

                            report("⚡ Audio streamed! Finding viral moments with AI...", 0.25, "transcribe")
                            eff_dur = target_sec if (target_sec and target_sec < dur_total) else dur_total
                            tr = transcribe(wav, opts.get("whisper_model", "base"), opts.get("language") or None, eff_dur, stage("transcribe", 0.25, 0.6))
                            (job_dir / "transcript.json").write_text(json.dumps(tr), encoding="utf-8")

                            energy = energy_per_second(wav) if wav.exists() else []
                            clips, notes = find_clips(tr["words"], eff_dur, energy, opts, stage("analyze", 0.6, 0.75))
                            for n, c in enumerate(clips, 1):
                                c["n"] = n
                                c["duration"] = round(c["end"] - c["start"], 1)
                                c["start"], c["end"] = round(c["start"], 2), round(c["end"], 2)
                                c["text"] = c["text"][:1500]

                            vinfo = {
                                "width": info.get("width") or 1920,
                                "height": info.get("height") or 1080,
                                "duration": dur_total,
                                "has_audio": True,
                                "title": title,
                                "source": "online_stream",
                                "stream_url": url,
                                "is_stream_slice": True,
                            }
                            return {
                                "video": vinfo,
                                "clips": clips,
                                "notes": notes + ["⚡ Fast Online Mode: Streamed audio directly without downloading the full video!"],
                                "language": tr.get("language", "en"),
                                "is_stream_slice": True,
                            }
            except Exception as e:
                print(f"[clipforge] Fast stream mode skipped ({e}), falling back to standard download...")

        source, title = download(url, job_dir, int(opts.get("live_minutes", 10)), stage("download", 0.0, 0.2),
                                 int(opts.get("source_height", 1080)), opts.get("cookies_browser", ""),
                                 opts.get("cookies_file", ""))
    vinfo = probe(source)
    need_space(job_dir, int(vinfo["duration"] * 40_000) + 500_000_000)  # audio copy + rendered clips
    wav = job_dir / "audio.wav"
    tj = job_dir / "transcript.json"
    if tj.exists():
        tr = json.loads(tj.read_text(encoding="utf-8"))
    else:
        report("Extracting audio...", 0.2, "transcribe")
        if vinfo["has_audio"]:
            extract_audio(source, wav)
            tr = transcribe(wav, opts.get("whisper_model", "base"), opts.get("language") or None, vinfo["duration"],
                            stage("transcribe", 0.2, 0.6))
        else:
            tr = {"language": "", "words": []}
        tj.write_text(json.dumps(tr), encoding="utf-8")
    report("Measuring audio energy...", 0.6, "analyze")
    energy = energy_per_second(wav) if wav.exists() else []
    clips, notes = find_clips(tr["words"], vinfo["duration"], energy, opts, stage("analyze", 0.6, 0.75))
    for n, c in enumerate(clips, 1):
        c["n"] = n
        c["duration"] = round(c["end"] - c["start"], 1)
        c["start"], c["end"] = round(c["start"], 2), round(c["end"], 2)
        c["text"] = c["text"][:1500]
        c.pop("first", None)
        c.pop("llm", None)
        c.pop("heur", None)
    return {"video": {**vinfo, "title": title, "source": source.name}, "clips": clips, "notes": notes,
            "language": tr.get("language", "")}
