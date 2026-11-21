"""ClipForge local web server.  Run:  python server.py   then open http://localhost:8787"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

import core

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("CLIPFORGE_DATA", ROOT / "data")).resolve()
(DATA / "jobs").mkdir(parents=True, exist_ok=True)

# Write cookies.txt from env var if present (for cloud deployments like Render)
if os.environ.get("YOUTUBE_COOKIES"):
    try:
        (DATA / "cookies.txt").write_text(os.environ["YOUTUBE_COOKIES"].strip(), encoding="utf-8")
    except Exception:
        pass

app = FastAPI(title="ClipForge")
JOBS: dict[str, dict] = {}
JOB_LOCK = threading.Lock()
WORKER = threading.Semaphore(1)  # one heavy job at a time
RENDER_LOCKS: dict[str, threading.Lock] = {}
CANCEL_EVENTS: dict[str, threading.Event] = {}


def jdir(jid: str) -> Path:
    if not jid.isalnum():
        raise HTTPException(404, "Unknown job")
    return DATA / "jobs" / jid


def save(job: dict):
    with JOB_LOCK:
        (jdir(job["id"]) / "job.json").write_text(json.dumps(job, indent=1), encoding="utf-8")


def load_jobs():
    for f in (DATA / "jobs").glob("*/job.json"):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
            if j.get("status") in ("queued", "running"):
                j["status"], j["error"] = "error", "Interrupted when the server stopped. Start it again."
            JOBS[j["id"]] = j
        except Exception:
            pass


def worker(jid: str, url: str, source: Path | None, opts: dict):
    job = JOBS[jid]
    ev = CANCEL_EVENTS.get(jid)

    def report(msg, frac=None, stage=None):
        if ev and ev.is_set():
            raise RuntimeError("Project stopped by user.")
        job["message"] = msg
        if stage:
            job["stage"] = stage
        if frac is not None:
            job["progress"] = round(max(job["progress"], min(frac, 0.99)), 3)

    if ev and ev.is_set():
        job.update(status="cancelled", message="Stopped by user", error="Stopped by user")
        save(job)
        return

    with WORKER:
        if ev and ev.is_set():
            job.update(status="cancelled", message="Stopped by user", error="Stopped by user")
            save(job)
            return
        job["status"] = "running"
        try:
            res = core.run_pipeline(jdir(jid), opts, report, source=source, url=url, title=job.get("title", ""))
            job.update(res)
            job["title"] = res["video"]["title"] or job.get("title", "")
            # render the preview quality for every clip
            quals = opts["qualities"]
            total, done = len(job["clips"]) * len(quals), 0
            for c in job["clips"]:
                for q in quals:
                    report(f"Rendering clip {c['n']} of {len(job['clips'])} ({q})...", 0.75 + 0.2 * done / max(1, total), "render")
                    render(jid, c["n"], q)
                    done += 1
            dest = opts.get("save_to", "")
            if dest:
                report("Saving clips to " + dest + "...", 0.96, "render")
                try:
                    job["saved_to"] = save_outputs(jid, dest, job)
                    if opts.get("remove_local"):
                        for f in (jdir(jid) / "clips").glob("clip*.mp4"):
                            f.unlink()
                        job["local_deleted"] = True
                except Exception as e:
                    job["save_error"] = str(e)[-400:]
            # Auto-upload to Google Drive if connected
            try:
                import drive
                if drive.is_connected():
                    report("Uploading clips to Google Drive...", 0.97, "render")
                    quals = opts.get("qualities", ["720p"])
                    clip_files = [jdir(jid) / "clips" / f"clip{c['n']:02d}_{q}.mp4"
                                  for c in job.get("clips", []) for q in quals
                                  if (jdir(jid) / "clips" / f"clip{c['n']:02d}_{q}.mp4").exists()]
                    if clip_files:
                        up_res = drive.upload_clips(jid, job.get("title", "clip"), clip_files)
                        job["drive_uploaded"] = True
                        job["drive_folder_id"] = up_res.get("folder_id", "")
                        job.setdefault("notes", []).append(f"✅ Saved to Google Drive (ClipForge folder).")
            except Exception as e:
                print(f"[clipforge drive upload error] {e}")
                job.setdefault("notes", []).append(f"Google Drive note: {str(e)[:150]}")

            # Auto-publish to YouTube Shorts if enabled & connected
            if opts.get("auto_publish_youtube"):
                try:
                    import youtube_publisher
                    if youtube_publisher.is_connected():
                        report("Auto-publishing clips to YouTube Shorts...", 0.98, "render")
                        privacy = opts.get("youtube_privacy", "public")
                        sched_mode = opts.get("youtube_schedule", "instant")
                        now = datetime.datetime.now(datetime.timezone.utc)
                        interval_hours = {"interval_2h": 2, "interval_4h": 4, "interval_6h": 6, "daily": 24}.get(sched_mode, 0)

                        for idx, c in enumerate(job.get("clips", [])):
                            best_qual = next((q for q in ("1080p", "720p", "480p") if (jdir(jid) / "clips" / f"clip{c['n']:02d}_{q}.mp4").exists()), None)
                            if not best_qual:
                                continue
                            clip_file = jdir(jid) / "clips" / f"clip{c['n']:02d}_{best_qual}.mp4"
                            meta = youtube_publisher.generate_metadata(
                                clip_title=c.get("title", f"Clip {c['n']}"),
                                clip_text=c.get("text", ""),
                                source_title=job.get("title", ""),
                                hf_token=opts.get("hf_token", ""),
                            )
                            c["youtube_metadata"] = meta
                            pub_time = None
                            if interval_hours > 0:
                                pub_time = now + datetime.timedelta(hours=(idx + 1) * interval_hours)

                            up_res = youtube_publisher.upload_short(
                                video_path=clip_file,
                                title=meta["title"],
                                description=meta["description"],
                                tags=meta["tags"],
                                privacy_status=privacy,
                                publish_at=pub_time,
                            )
                            c["youtube_url"] = up_res["url"]
                            c["youtube_video_id"] = up_res["video_id"]
                            c["youtube_privacy"] = privacy
                            c["youtube_scheduled"] = up_res.get("scheduled")
                            c["youtube_status"] = "published" if not pub_time else "scheduled"
                            job.setdefault("notes", []).append(f"Published Short {c['n']}: {up_res['url']}")
                except Exception as e:
                    print(f"[clipforge youtube auto-publish error] {e}")
                    job.setdefault("notes", []).append(f"YouTube auto-publish note: {str(e)[:150]}")

            if opts.get("delete_source", True):
                for f in list(jdir(jid).glob("source.*")) + [jdir(jid) / "audio.wav"]:
                    f.unlink(missing_ok=True)
            job.update(status="done", progress=1.0, stage="done", message="Done")
        except Exception as e:
            if (ev and ev.is_set()) or "stopped by user" in str(e).lower():
                job.update(status="cancelled", error="Project stopped by user", message="Stopped by user")
                return
            msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))[-900:]
            low = msg.lower()
            if "not a bot" in low or "confirm you" in low:
                msg = ("YouTube is asking for a sign-in check before it lets this video be downloaded.\n\n"
                       "Fix: open Advanced, set \"YouTube sign-in\" to the browser where you are logged in to YouTube "
                       "(Chrome or Firefox work best) and try again. If your Mac asks to allow access to the browser's keychain, "
                       "choose Allow. Or download the video yourself and upload the file instead.")
            elif "format is not available" in low or "only images" in low or "no video formats" in low:
                msg = ("YouTube did not hand over any video formats. This almost always means the small helper YouTube now needs "
                       "(Deno) is missing.\n\nFix: in Terminal run  curl -fsSL https://deno.land/install.sh | sh  then close Terminal, "
                       "open a new one, run  pip install -U \"yt-dlp[default]\"  and start ClipForge again.\n"
                       "Or download the video yourself and upload the file.\n\nDetails: " + msg[:300])
            elif "locate the files on the hub" in low or "name or service not known" in low and "huggingface" in low:
                msg += "\n\nHint: the speech model is downloaded once on first use, so connect to the internet and try again."
            elif "no space left" in low or "errno 28" in low:
                msg += "\n\nHint: the drive is full. Free some space or point CLIPFORGE_DATA at a bigger drive."
            elif "yt-dlp" in low or "[youtube]" in low:
                msg += "\n\nHint: update the downloader with  pip install -U yt-dlp  and restart. For restricted videos, download the file yourself and upload it instead."
            job.update(status="error", error=msg, message="Failed")
        finally:
            for k in ("hf_token",):
                job.get("opts", {}).pop(k, None)
            save(job)


def friendly(job: dict, c: dict, q: str) -> str:
    return f"{core.slug(job.get('title', 'clip'), 24)}_clip{c['n']}_{core.slug(c.get('title', ''), 30)}_{q}.mp4"


def is_local_dest(dest: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", dest) or dest.startswith(("/", "~", ".")) or Path(dest).expanduser().is_dir())


def save_outputs(jid: str, dest: str, job: dict) -> str:
    """Copy finished clips to a folder (Google Drive for Desktop, external disk...) or an rclone remote."""
    d = jdir(jid)
    sub = f"{core.slug(job.get('title', 'clip'), 40)}_{jid}"
    pairs = [(f, friendly(job, c, q)) for c in job["clips"] for q in core.QUALITIES
             if (f := d / "clips" / f"clip{c['n']:02d}_{q}.mp4").exists()]
    if not pairs:
        raise RuntimeError("No rendered clips to save.")
    if is_local_dest(dest):
        target = Path(dest).expanduser() / sub
        target.mkdir(parents=True, exist_ok=True)
        for f, name in pairs:
            shutil.copyfile(f, target / name)
        return str(target)
    rc = shutil.which("rclone")
    if not rc:
        raise RuntimeError("rclone is not installed. Install it from https://rclone.org/install/ and run 'rclone config' once "
                           "to connect Google Drive (see README), or type a folder path instead.")
    target = dest.rstrip("/") + "/" + sub
    for f, name in pairs:
        r = subprocess.run([rc, "copyto", str(f), f"{target}/{name}"], capture_output=True, text=True, errors="replace")
        if r.returncode:
            raise RuntimeError("rclone: " + (r.stderr or r.stdout)[-400:])
    return target


def render(jid: str, n: int, quality: str) -> Path:
    if quality not in core.QUALITIES:
        raise HTTPException(400, f"quality must be one of {list(core.QUALITIES)}")
    job = JOBS.get(jid)
    if not job or not (1 <= n <= len(job.get("clips", []))):
        raise HTTPException(404, "Clip not found")
    clip = job["clips"][n - 1]
    d = jdir(jid)
    out = d / "clips" / f"clip{n:02d}_{quality}.mp4"
    lock = RENDER_LOCKS.setdefault(str(out), threading.Lock())
    with lock:
        if out.exists() and out.stat().st_size > 0:
            return out
        out.parent.mkdir(exist_ok=True)
        if job.get("local_deleted"):
            raise HTTPException(410, "This clip was moved to your cloud/folder and removed from this computer.")
        if not (d / job["video"]["source"]).exists():
            raise HTTPException(410, f"The source video was deleted to save space, and {quality} was not rendered. "
                                     "Re-run the project and tick that quality under Storage.")
        tr = json.loads((d / "transcript.json").read_text(encoding="utf-8"))
        src = d / job["video"]["source"]
        tmp = out.with_suffix(".part.mp4")
        core.render_clip(src, clip, tr["words"], job["video"], quality, tmp, job["opts"], d / "clips" / f"work{n:02d}")
        tmp.replace(out)
    return out


@app.get("/", response_class=HTMLResponse)
def index():
    return (ROOT / "index.html").read_text(encoding="utf-8")


@app.post("/api/jobs")
def create_job(
    url: str = Form(""), file: UploadFile | None = File(None), num_clips: int = Form(5), min_len: int = Form(20),
    max_len: int = Form(60), layout: str = Form("fill"), captions: bool = Form(True), hook: bool = Form(True),
    uppercase: bool = Form(True), caption_color: str = Form("gold"), whisper_model: str = Form("base"),
    language: str = Form(""), live_minutes: int = Form(10), preview_quality: str = Form("720p"),
    hf_token: str = Form(""), llm_model: str = Form(""), qualities: str = Form("720p,1080p"),
    source_height: int = Form(1080), delete_source: bool = Form(True), save_to: str = Form(""),
    remove_local: bool = Form(False), cookies_browser: str = Form(""), cookies_file: str = Form(""),
    auto_publish_youtube: bool = Form(False), youtube_privacy: str = Form("public"), youtube_schedule: str = Form("instant"),
):
    url = url.strip()
    if not url and not (file and file.filename):
        raise HTTPException(400, "Paste a video link or choose a file.")
    if min_len < 5 or max_len < min_len or max_len > 180:
        raise HTTPException(400, "Clip length must satisfy 5 <= min <= max <= 180 seconds.")
    quals = [q for q in qualities.split(",") if q in core.QUALITIES] or ["720p"]
    dest = save_to.strip()
    if re.match(r"^https?://", dest, re.I):
        raise HTTPException(400, "That is a Google Drive web link, which ClipForge cannot write to. Use a folder on this Mac instead: "
                                 "install Google Drive for desktop and choose its 'ClipForge' folder from the suggestions, or use an rclone remote like gdrive:ClipForge.")
    if dest and is_local_dest(dest):
        pth = Path(dest).expanduser()
        if not (pth.exists() or pth.parent.exists()):
            raise HTTPException(400, f"The folder '{dest}' was not found. Check the path (the parent folder must exist).")
    if remove_local and not save_to.strip():
        raise HTTPException(400, "To remove local copies, also enter where to save the clips.")
    cookies_browser, cookies_file = cookies_browser.strip().lower(), cookies_file.strip()
    if not cookies_file and (DATA / "cookies.txt").is_file():
        cookies_file = str(DATA / "cookies.txt")
    if cookies_browser not in ("", "chrome", "firefox", "safari", "edge", "brave", "chromium", "opera", "vivaldi"):
        raise HTTPException(400, "Unknown browser for cookies.")
    if cookies_file and not Path(cookies_file).expanduser().is_file():
        raise HTTPException(400, "The cookies.txt file was not found at that path.")
    local = None
    if not (file and file.filename) and not core.is_url(url):
        local = Path(url).expanduser()
        if not local.is_file():
            raise HTTPException(400, "That is not a web link or an existing file path.")
    jid = uuid.uuid4().hex[:10]
    d = jdir(jid)
    d.mkdir(parents=True)
    source = None
    title = url
    if file and file.filename:
        ext = Path(file.filename).suffix.lower() or ".mp4"
        source = d / f"source{ext}"
        with open(source, "wb") as f:
            shutil.copyfileobj(file.file, f)
        title = Path(file.filename).stem
    elif local is not None:
        p = local
        source = d / f"source{p.suffix.lower() or '.mp4'}"
        shutil.copyfile(p, source)
        title = p.stem
    opts = dict(num_clips=max(1, min(num_clips, 20)), min_len=min_len, max_len=max_len, layout=layout,
                captions=captions, hook=hook, uppercase=uppercase, caption_color=caption_color,
                whisper_model=whisper_model, language=language.strip(), live_minutes=max(1, min(live_minutes, 360)),
                qualities=quals, preview_quality="720p" if "720p" in quals else quals[0],
                source_height=source_height if source_height in (480, 720, 1080) else 1080,
                delete_source=delete_source, save_to=save_to.strip(), remove_local=remove_local,
                hf_token=hf_token.strip(), llm_model=llm_model.strip(),
                cookies_browser=cookies_browser, cookies_file=str(Path(cookies_file).expanduser()) if cookies_file else "",
                auto_publish_youtube=auto_publish_youtube, youtube_privacy=youtube_privacy, youtube_schedule=youtube_schedule)
    job = {"id": jid, "title": title, "url": url, "status": "queued", "stage": "download", "progress": 0.0,
           "message": "Waiting to start...", "error": "", "created": time.time(), "clips": [],
           "opts": {k: v for k, v in opts.items() if k != "hf_token"}}
    JOBS[jid] = job
    save(job)
    CANCEL_EVENTS[jid] = threading.Event()
    threading.Thread(target=worker, args=(jid, url if core.is_url(url) else "", source, opts), daemon=True).start()
    return {"id": jid}


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str):
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "Unknown job")
    ev = CANCEL_EVENTS.get(jid)
    if ev:
        ev.set()
    job.update(status="cancelled", message="Stopped by user", error="Stopped by user")
    save(job)
    return {"ok": True}


@app.post("/api/jobs/{jid}/retry")
def retry_job(jid: str):
    job = JOBS.get(jid)
    if not job or job.get("status") != "error":
        raise HTTPException(400, "Only failed projects can be retried.")
    srcs = [f for f in sorted(jdir(jid).glob("source.*")) if f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi")]
    if not srcs:
        raise HTTPException(400, "The downloaded video is no longer on this computer. Start a new project.")
    job.update(status="queued", stage="analyze", progress=0.2, message="Retrying...", error="", clips=[], notes=[])
    save(job)
    threading.Thread(target=worker, args=(jid, "", srcs[0], dict(job["opts"])), daemon=True).start()
    return {"id": jid}


@app.get("/api/destinations")
def destinations():
    """Local folders where Google Drive (or other synced storage) is mounted, so users can pick one."""
    home = Path.home()
    pats = [home / "Library/CloudStorage"]
    out = []
    for base in pats:
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if d.name.startswith(("GoogleDrive", "OneDrive", "Dropbox")):
                    mine = d / "My Drive"
                    out.append(str((mine if mine.is_dir() else d) / "ClipForge"))
    for extra in (home / "Google Drive", home / "My Drive", home / "Dropbox", home / "OneDrive"):
        if extra.is_dir():
            out.append(str(extra / "ClipForge"))
    return out


@app.get("/api/health")
def api_health():
    return core.health()


@app.get("/api/jobs")
def list_jobs():
    return sorted(({k: j.get(k) for k in ("id", "title", "status", "progress", "created")} | {"clips": len(j.get("clips", []))}
                   for j in JOBS.values()), key=lambda x: x["created"], reverse=True)


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    if jid not in JOBS:
        raise HTTPException(404, "Unknown job")
    j = dict(JOBS[jid])
    d = jdir(jid)
    if j.get("status") == "done" and j.get("video"):
        src_ok = (d / j["video"]["source"]).exists()
        j["clips"] = [{**c, "available": [q for q in core.QUALITIES
                                          if src_ok or (d / "clips" / f"clip{c['n']:02d}_{q}.mp4").exists()]}
                      for c in j["clips"]]
    j["can_retry"] = j.get("status") == "error" and any(f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi")
                                                       for f in d.glob("source.*"))
    j["size_mb"] = round(sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1e6) if d.exists() else 0
    return j


@app.get("/api/jobs/{jid}/clips/{n}/video")
def clip_video(jid: str, n: int, quality: str = "720p", download: bool = False):
    path = render(jid, n, quality)
    clip = JOBS[jid]["clips"][n - 1]
    name = f"{core.slug(JOBS[jid].get('title', 'clip'), 24)}_clip{n}_{core.slug(clip.get('title', ''), 30)}_{quality}.mp4"
    return FileResponse(path, media_type="video/mp4", filename=name if download else None)


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    JOBS.pop(jid, None)
    shutil.rmtree(jdir(jid), ignore_errors=True)
    return {"ok": True}


# ----------------------------------------------------------------------------- Google Drive & Storage
def _callback_uri(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    if "onrender.com" in str(host) or proto == "https":
        return f"https://{host}/api/drive/callback"
    if "127.0.0.1" in str(host) or "localhost" in str(host):
        return "http://localhost:8787/api/drive/callback"
    return f"{str(request.base_url).rstrip('/')}/api/drive/callback"


@app.get("/api/drive/status")
def drive_status():
    import drive
    return drive.get_status()


@app.get("/api/drive/auth")
def drive_auth(request: Request):
    import drive
    cb = _callback_uri(request)
    url = drive.get_auth_url(cb)
    if not url:
        raise HTTPException(400, "Google Drive credentials not configured. Please set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.")
    return {"url": url}


@app.get("/api/drive/callback")
def drive_callback(code: str, request: Request):
    import drive
    cb = _callback_uri(request)
    ok, err_msg = drive.handle_callback(code, cb)
    if not ok:
        return HTMLResponse(f"<body style='font-family:sans-serif;padding:40px;text-align:center;'><h2>❌ Could not connect Google Drive</h2><p style='color:#c00;'>{err_msg}</p><p>Please check your credentials or try again.</p></body>")
    return HTMLResponse("<body style='font-family:sans-serif;padding:40px;text-align:center;'><h2>✅ Google Drive Connected!</h2><p>All finished clips will now be saved to your Google Drive automatically.</p><p>You can close this tab now.</p><script>setTimeout(() => window.close(), 2500);</script></body>")


@app.post("/api/drive/disconnect")
def drive_disconnect():
    import drive
    drive.disconnect()
    return {"ok": True}


# ----------------------------------------------------------------------------- YouTube Shorts
def _yt_callback_uri(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    if "onrender.com" in str(host) or proto == "https":
        return f"https://{host}/api/youtube/callback"
    if "127.0.0.1" in str(host) or "localhost" in str(host):
        return "http://localhost:8787/api/youtube/callback"
    return f"{str(request.base_url).rstrip('/')}/api/youtube/callback"


@app.get("/api/youtube/status")
def youtube_status():
    import youtube_publisher
    return youtube_publisher.get_status()


@app.get("/api/youtube/auth")
def youtube_auth(request: Request):
    import youtube_publisher
    cb = _yt_callback_uri(request)
    url = youtube_publisher.get_auth_url(cb)
    if not url:
        raise HTTPException(400, "Google credentials not configured. Please set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.")
    return {"url": url}


@app.get("/api/youtube/callback")
def youtube_callback(code: str, request: Request):
    import youtube_publisher
    cb = _yt_callback_uri(request)
    ok, err_msg = youtube_publisher.handle_callback(code, cb)
    if not ok:
        return HTMLResponse(f"<body style='font-family:sans-serif;padding:40px;text-align:center;'><h2>❌ Could not connect YouTube Channel</h2><p style='color:#c00;'>{err_msg}</p><p>Please check your credentials or try again.</p></body>")
    return HTMLResponse("<body style='font-family:sans-serif;padding:40px;text-align:center;'><h2>✅ YouTube Channel Connected!</h2><p>ClipForge is now authorized to publish YouTube Shorts to your channel.</p><p>You can close this tab now.</p><script>setTimeout(() => window.close(), 2500);</script></body>")


@app.post("/api/youtube/disconnect")
def youtube_disconnect():
    import youtube_publisher
    youtube_publisher.disconnect()
    return {"ok": True}


@app.post("/api/jobs/{jid}/clips/{n}/publish-youtube")
def manual_publish_clip_youtube(jid: str, n: int, title: str = Form(""), privacy: str = Form("public")):
    import youtube_publisher
    if not youtube_publisher.is_connected():
        raise HTTPException(400, "YouTube account is not connected. Connect your YouTube channel first.")
    job = JOBS.get(jid)
    if not job or not (1 <= n <= len(job.get("clips", []))):
        raise HTTPException(404, "Clip not found")
    c = job["clips"][n - 1]
    best_qual = next((q for q in ("1080p", "720p", "480p") if (jdir(jid) / "clips" / f"clip{n:02d}_{q}.mp4").exists()), None)
    if not best_qual:
        best_qual = "720p"
        render(jid, n, best_qual)
    clip_file = jdir(jid) / "clips" / f"clip{n:02d}_{best_qual}.mp4"
    meta = youtube_publisher.generate_metadata(
        clip_title=title or c.get("title", f"Clip {n}"),
        clip_text=c.get("text", ""),
        source_title=job.get("title", ""),
    )
    if title:
        meta["title"] = f"{title} #Shorts" if "#shorts" not in title.lower() else title
    up_res = youtube_publisher.upload_short(
        video_path=clip_file,
        title=meta["title"],
        description=meta["description"],
        tags=meta["tags"],
        privacy_status=privacy,
    )
    c["youtube_url"] = up_res["url"]
    c["youtube_video_id"] = up_res["video_id"]
    c["youtube_privacy"] = privacy
    c["youtube_status"] = "published"
    c["youtube_metadata"] = meta
    save(job)
    return up_res


@app.get("/api/storage")
def storage_info():
    total_bytes = 0
    jobs_summary = []
    for jid, j in JOBS.items():
        d = jdir(jid)
        if d.exists():
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            total_bytes += size
            has_raw = any(d.glob("source.*"))
            has_clips = any((d / "clips").glob("clip*.mp4")) if (d / "clips").is_dir() else False
            jobs_summary.append({
                "id": jid,
                "title": j.get("title", ""),
                "size_mb": round(size / 1e6, 1),
                "drive_uploaded": bool(j.get("drive_uploaded")),
                "has_raw": has_raw,
                "has_clips": has_clips,
            })
    disk_free = shutil.disk_usage(DATA).free
    return {
        "used_mb": round(total_bytes / 1e6, 1),
        "free_mb": round(disk_free / 1e6, 1),
        "jobs": sorted(jobs_summary, key=lambda x: x["size_mb"], reverse=True),
    }


@app.post("/api/jobs/{jid}/cleanup")
def cleanup_job(jid: str):
    """Keep preview clips, delete raw video & work cache to free disk space."""
    d = jdir(jid)
    if not d.exists():
        raise HTTPException(404, "Job not found")
    freed = 0
    for f in list(d.glob("source.*")) + [d / "audio.wav"]:
        if f.is_file():
            freed += f.stat().st_size
            f.unlink(missing_ok=True)
    clips_dir = d / "clips"
    if clips_dir.is_dir():
        for wd in clips_dir.glob("work*"):
            if wd.is_dir():
                freed += sum(f.stat().st_size for f in wd.rglob("*") if f.is_file())
                shutil.rmtree(wd, ignore_errors=True)
    return {"freed_mb": round(freed / 1e6, 1)}


@app.post("/api/storage/cleanup-all")
def cleanup_all_storage():
    """Clean heavy temp files across all jobs, keeping clip previews."""
    total_freed = 0
    for jid in list(JOBS.keys()):
        d = jdir(jid)
        if not d.exists():
            continue
        for f in list(d.glob("source.*")) + [d / "audio.wav"]:
            if f.is_file():
                total_freed += f.stat().st_size
                f.unlink(missing_ok=True)
        clips_dir = d / "clips"
        if clips_dir.is_dir():
            for wd in clips_dir.glob("work*"):
                if wd.is_dir():
                    total_freed += sum(f.stat().st_size for f in wd.rglob("*") if f.is_file())
                    shutil.rmtree(wd, ignore_errors=True)
    return {"freed_mb": round(total_freed / 1e6, 1)}


load_jobs()

if __name__ == "__main__":
    host = os.environ.get("CLIPFORGE_HOST", "0.0.0.0" if (os.environ.get("PORT") or os.environ.get("RENDER") or os.environ.get("SPACE_ID")) else "127.0.0.1")
    port = int(os.environ.get("PORT") or os.environ.get("CLIPFORGE_PORT") or 8787)
    print(f"\n  ClipForge is running at http://{host}:{port}\n")
    for w in core.health()["warnings"]:
        print("  WARNING:", w, "\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
