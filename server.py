"""ClipForge local web server.  Run:  python server.py   then open http://localhost:8787"""
from __future__ import annotations

import concurrent.futures
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
WORKER = threading.Semaphore(1)  # one heavy render at a time
RENDER_LOCKS: dict[str, threading.Lock] = {}
CANCEL_EVENTS: dict[str, threading.Event] = {}

# Sequential FIFO Queue for batch processing multiple videos
JOB_QUEUE: list[tuple[str, str, Path | None, dict]] = []
CURRENT_JOB_ID: str | None = None
QUEUE_LOCK = threading.Lock()


def _run_next_in_queue():
    global CURRENT_JOB_ID
    with QUEUE_LOCK:
        if CURRENT_JOB_ID is not None:
            return
        if not JOB_QUEUE:
            return
        next_job_args = JOB_QUEUE.pop(0)
        CURRENT_JOB_ID = next_job_args[0]
        jid = next_job_args[0]
        if jid in JOBS:
            JOBS[jid]["status"] = "running"
            JOBS[jid]["message"] = "⚡ Connecting to stream & checking captions..."
            JOBS[jid]["stage"] = "transcribe"
            save(JOBS[jid])
        threading.Thread(target=_worker_wrapper, args=next_job_args, daemon=True).start()


def _worker_wrapper(jid: str, url: str, source: Path | None, opts: dict):
    global CURRENT_JOB_ID
    try:
        worker(jid, url, source, opts)
    finally:
        with QUEUE_LOCK:
            CURRENT_JOB_ID = None
        _run_next_in_queue()


def jdir(jid: str) -> Path:
    if not jid.isalnum():
        raise HTTPException(404, "Unknown job")
    return DATA / "jobs" / jid


def save(job: dict):
    with JOB_LOCK:
        d = jdir(job["id"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "job.json").write_text(json.dumps(job, indent=1), encoding="utf-8")


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
    _last_save = [0.0]

    def report(msg, frac=None, stage=None):
        if ev and ev.is_set():
            raise RuntimeError("Project stopped by user.")
        job["message"] = msg
        if stage:
            job["stage"] = stage
        if frac is not None:
            job["progress"] = round(max(job["progress"], min(frac, 0.99)), 3)
        now = time.time()
        if stage or now - _last_save[0] > 1.0:
            try:
                save(job)
                _last_save[0] = now
            except Exception:
                pass

    if ev and ev.is_set():
        job.update(status="cancelled", message="Stopped by user", error="Stopped by user")
        save(job)
        return

    job["status"] = "running"
    job["message"] = "⚡ Connecting to stream & analyzing..."
    job["progress"] = 0.02
    save(job)
    try:
        res = core.run_pipeline(jdir(jid), opts, report, source=source, url=url, title=job.get("title", ""))
        job.update(res)
        job["title"] = res["video"]["title"] or job.get("title", "")
        # render the preview quality for every clip in parallel (2 workers on Apple Silicon)
        quals = opts["qualities"]
        total, done = len(job["clips"]) * len(quals), 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(render, jid, c["n"], q): (c["n"], q) for c in job["clips"] for q in quals}
            for fut in concurrent.futures.as_completed(futures):
                try:
                    fut.result()
                except Exception as ex:
                    print(f"[clipforge render error] {ex}")
                done += 1
                report(f"Rendering clips ({done}/{total})...", 0.75 + 0.2 * done / max(1, total), "render")
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
                    interval_hours = {
                        "interval_2h": 2,
                        "interval_4h": 4,
                        "interval_6h": 6,
                        "interval_8h": 8,
                        "interval_12h": 12,
                        "daily": 24,
                    }.get(sched_mode, 0)

                    # Smart Collision-Free Scheduling:
                    # Automatically checks the latest scheduled Short in the channel
                    # so multiple videos queue & drop one after another across days with ZERO clashes!
                    if interval_hours > 0:
                        latest_sched = youtube_publisher.get_latest_scheduled_time(JOBS)
                        start_delay_hours = int(opts.get("youtube_start_delay", 0))
                        if latest_sched > now:
                            base_time = latest_sched
                        else:
                            base_time = now + datetime.timedelta(hours=start_delay_hours)
                    else:
                        base_time = now

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
                            clip_index=idx + 1,
                        )
                        c["youtube_metadata"] = meta
                        pub_time = None
                        if interval_hours > 0:
                            pub_time = base_time + datetime.timedelta(hours=(idx + 1) * interval_hours)

                        try:
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
                            save(job)
                        except Exception as e:
                            err_str = str(e)
                            print(f"[clipforge youtube auto-publish clip {c['n']} error] {err_str}")
                            if "uploadLimitExceeded" in err_str:
                                job.setdefault("notes", []).append("⚠️ YouTube Daily Upload Limit Reached: YouTube limits channels to ~10-15 uploads per 24 hours. The remaining clips are saved locally and ready to schedule.")
                                break
                            else:
                                job.setdefault("notes", []).append(f"Clip {c['n']} upload note: {err_str[:120]}")
            except Exception as e:
                print(f"[clipforge youtube auto-publish error] {e}")
                job.setdefault("notes", []).append(f"YouTube auto-publish note: {str(e)[:150]}")

        # Auto-publish to Instagram Reels if enabled & connected
        if opts.get("auto_publish_instagram"):
            try:
                import meta_publisher
                if meta_publisher.is_connected() and meta_publisher.load_token_data().get("instagram_account_id"):
                    report("Auto-publishing clips to Instagram Reels...", 0.98, "render")
                    for idx, c in enumerate(job.get("clips", [])):
                        best_qual = next((q for q in ("1080p", "720p", "480p") if (jdir(jid) / "clips" / f"clip{c['n']:02d}_{q}.mp4").exists()), None)
                        if not best_qual:
                            continue
                        clip_file = jdir(jid) / "clips" / f"clip{c['n']:02d}_{best_qual}.mp4"
                        meta = meta_publisher.generate_reels_metadata(
                            clip_title=c.get("title", f"Clip {c['n']}"),
                            clip_text=c.get("text", ""),
                            source_title=job.get("title", ""),
                            clip_index=idx + 1,
                        )
                        c["instagram_metadata"] = meta
                        try:
                            ig_res = meta_publisher.upload_instagram_reel(
                                video_path=clip_file,
                                caption=meta["caption"],
                                share_to_feed=True,
                            )
                            c["instagram_url"] = ig_res["url"]
                            c["instagram_id"] = ig_res["id"]
                            c["instagram_status"] = "published"
                            job.setdefault("notes", []).append(f"Published Instagram Reel {c['n']}: {ig_res['url']}")
                            save(job)
                        except Exception as e:
                            err_str = str(e)
                            print(f"[clipforge instagram auto-publish clip {c['n']} error] {err_str}")
                            job.setdefault("notes", []).append(f"Clip {c['n']} Instagram note: {err_str[:120]}")
            except Exception as e:
                print(f"[clipforge instagram auto-publish error] {e}")
                job.setdefault("notes", []).append(f"Instagram auto-publish note: {str(e)[:150]}")

        # Auto-publish to Facebook Reels if enabled & connected
        if opts.get("auto_publish_facebook"):
            try:
                import meta_publisher
                if meta_publisher.is_connected() and meta_publisher.load_token_data().get("page_id"):
                    report("Auto-publishing clips to Facebook Reels...", 0.99, "render")
                    sched_mode = opts.get("meta_schedule", "instant")
                    now = datetime.datetime.now(datetime.timezone.utc)
                    fb_interval_hours = {
                        "interval_2h": 2,
                        "interval_4h": 4,
                        "interval_6h": 6,
                        "interval_8h": 8,
                        "interval_12h": 12,
                        "daily": 24,
                    }.get(sched_mode, 0)

                    if fb_interval_hours > 0:
                        latest_sched = meta_publisher.get_latest_scheduled_time(JOBS)
                        base_time = latest_sched if latest_sched > now else now
                    else:
                        base_time = now

                    for idx, c in enumerate(job.get("clips", [])):
                        best_qual = next((q for q in ("1080p", "720p", "480p") if (jdir(jid) / "clips" / f"clip{c['n']:02d}_{q}.mp4").exists()), None)
                        if not best_qual:
                            continue
                        clip_file = jdir(jid) / "clips" / f"clip{c['n']:02d}_{best_qual}.mp4"
                        meta = meta_publisher.generate_reels_metadata(
                            clip_title=c.get("title", f"Clip {c['n']}"),
                            clip_text=c.get("text", ""),
                            source_title=job.get("title", ""),
                            clip_index=idx + 1,
                        )
                        c["facebook_metadata"] = meta
                        pub_time = None
                        if fb_interval_hours > 0:
                            pub_time = base_time + datetime.timedelta(hours=(idx + 1) * fb_interval_hours)

                        try:
                            fb_res = meta_publisher.upload_facebook_reel(
                                video_path=clip_file,
                                title=meta["title"],
                                description=meta["caption"],
                                scheduled_publish_time=pub_time,
                            )
                            c["facebook_url"] = fb_res["url"]
                            c["facebook_id"] = fb_res["id"]
                            c["facebook_scheduled"] = fb_res.get("scheduled")
                            c["facebook_status"] = "published" if not pub_time else "scheduled"
                            job.setdefault("notes", []).append(f"Published Facebook Reel {c['n']}: {fb_res['url']}")
                            save(job)
                        except Exception as e:
                            err_str = str(e)
                            print(f"[clipforge facebook auto-publish clip {c['n']} error] {err_str}")
                            job.setdefault("notes", []).append(f"Clip {c['n']} Facebook note: {err_str[:120]}")
            except Exception as e:
                print(f"[clipforge facebook auto-publish error] {e}")
                job.setdefault("notes", []).append(f"Facebook auto-publish note: {str(e)[:150]}")

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
        if job.get("is_stream_slice") or (job.get("video") and job["video"].get("is_stream_slice")):
            slice_path = d / "clips" / f"slice_{n:02d}.mp4"
            if not slice_path.exists():
                core.download_clip_slice(
                    url=job["video"]["stream_url"],
                    start=clip["start"],
                    duration=clip["duration"],
                    out_path=slice_path,
                    max_height=int(job["opts"].get("source_height", 1080)),
                    cookies_browser=job["opts"].get("cookies_browser", ""),
                    cookies_file=job["opts"].get("cookies_file", ""),
                )
            tr = json.loads((d / "transcript.json").read_text(encoding="utf-8"))
            tmp = out.with_suffix(".part.mp4")
            slice_clip = dict(clip, start=0.0, end=clip["duration"])
            slice_vinfo = dict(job["video"], **core.probe(slice_path))
            core.render_clip(slice_path, slice_clip, tr["words"], slice_vinfo, quality, tmp, job["opts"], d / "clips" / f"work{n:02d}", offset_words=clip["start"])
            tmp.replace(out)
            slice_path.unlink(missing_ok=True)
            return out

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
    auto_publish_instagram: bool = Form(False), auto_publish_facebook: bool = Form(False), meta_schedule: str = Form("instant"),
    cta: bool = Form(True), fast_stream: bool = Form(True),
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
    global CURRENT_JOB_ID
    raw_urls = [u.strip() for u in re.split(r"[\r\n]+", url) if u.strip()] if (url and not file) else []
    if not raw_urls and not (file and file.filename):
        raise HTTPException(400, "Paste a video link or choose a file.")

    opts = dict(num_clips=max(1, min(num_clips, 20)), min_len=min_len, max_len=max_len, layout=layout,
                captions=captions, hook=hook, uppercase=uppercase, caption_color=caption_color,
                whisper_model=whisper_model, language=language.strip(), live_minutes=max(1, min(live_minutes, 360)),
                qualities=quals, preview_quality="720p" if "720p" in quals else quals[0],
                source_height=source_height if source_height in (480, 720, 1080) else 1080,
                delete_source=delete_source, save_to=save_to.strip(), remove_local=remove_local,
                hf_token=hf_token.strip(), llm_model=llm_model.strip(),
                cookies_browser=cookies_browser, cookies_file=str(Path(cookies_file).expanduser()) if cookies_file else "",
                auto_publish_youtube=auto_publish_youtube, youtube_privacy=youtube_privacy, youtube_schedule=youtube_schedule,
                auto_publish_instagram=auto_publish_instagram, auto_publish_facebook=auto_publish_facebook, meta_schedule=meta_schedule,
                cta=cta, fast_stream=(fast_stream in (True, "true", "True", "1", 1)))

    created_ids = []
    items_to_process = raw_urls if raw_urls else [""]

    for idx, single_u in enumerate(items_to_process):
        jid = uuid.uuid4().hex[:10]
        d = jdir(jid)
        d.mkdir(parents=True, exist_ok=True)
        source = None
        title = single_u or "Uploaded video"
        if file and file.filename:
            ext = Path(file.filename).suffix.lower() or ".mp4"
            source = d / f"source{ext}"
            with open(source, "wb") as f:
                shutil.copyfileobj(file.file, f)
            title = Path(file.filename).stem

        CANCEL_EVENTS[jid] = threading.Event()
        with QUEUE_LOCK:
            is_first = (CURRENT_JOB_ID is None and len(JOB_QUEUE) == 0 and idx == 0)
            if is_first:
                job_st = "running"
                job_msg = "⚡ Connecting to stream & checking captions..."
                job_stage = "transcribe"
                job_prog = 0.02
            else:
                q_pos = len(JOB_QUEUE) + (1 if CURRENT_JOB_ID is not None else 0)
                job_st = "queued"
                job_msg = f"⏳ Queued (#{q_pos} in queue - will process automatically)"
                job_stage = "download"
                job_prog = 0.0

            job = {"id": jid, "title": title, "url": single_u, "status": job_st, "stage": job_stage, "progress": job_prog,
                   "message": job_msg, "error": "", "created": time.time() + idx * 0.001, "clips": [],
                   "opts": {k: v for k, v in opts.items() if k != "hf_token"}}
            JOBS[jid] = job
            save(job)
            created_ids.append(jid)

            if is_first:
                CURRENT_JOB_ID = jid
                threading.Thread(target=_worker_wrapper, args=(jid, single_u if core.is_url(single_u) else "", source, opts), daemon=True).start()
            else:
                JOB_QUEUE.append((jid, single_u if core.is_url(single_u) else "", source, opts))

    return {"id": created_ids[0], "ids": created_ids}


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str):
    global CURRENT_JOB_ID
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "Unknown job")
    ev = CANCEL_EVENTS.get(jid)
    if ev:
        ev.set()
    with QUEUE_LOCK:
        JOB_QUEUE[:] = [item for item in JOB_QUEUE if item[0] != jid]
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
    if jid not in JOBS or JOBS[jid].get("status") not in ("queued", "running"):
        p = jdir(jid) / "job.json"
        if p.exists():
            try:
                JOBS[jid] = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
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
        clip_index=n,
    )
    if title:
        meta["title"] = f"{title} #Shorts" if "#shorts" not in title.lower() else title
    try:
        up_res = youtube_publisher.upload_short(
            video_path=clip_file,
            title=meta["title"],
            description=meta["description"],
            tags=meta["tags"],
            privacy_status=privacy,
        )
    except Exception as e:
        err_str = str(e)
        if "uploadLimitExceeded" in err_str:
            raise HTTPException(400, "⚠️ YouTube Daily Upload Limit Reached on your channel (~10 uploads per 24 hours). Deleting videos does not reset this 24-hour limit on YouTube. Enable 'Advanced features' in YouTube Studio to unlock 100+ uploads per day.")
        raise HTTPException(500, f"YouTube upload failed: {err_str[:250]}")
    c["youtube_url"] = up_res["url"]
    c["youtube_video_id"] = up_res["video_id"]
    c["youtube_privacy"] = privacy
    c["youtube_status"] = "published"
    c["youtube_metadata"] = meta
    save(job)
    return up_res


@app.post("/api/jobs/{jid}/sync-youtube")
def sync_youtube_job(jid: str):
    import youtube_publisher
    if not youtube_publisher.is_connected():
        return {"ok": False, "detail": "YouTube not connected"}
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "Unknown job")
    changed = False
    for c in job.get("clips", []):
        vid = c.get("youtube_video_id")
        if vid:
            res = youtube_publisher.check_video_status(vid)
            if not res.get("exists") or res.get("status") == "deleted":
                c["youtube_status"] = "deleted"
                c["youtube_url"] = None
                changed = True
            elif res.get("status") == "live":
                c["youtube_status"] = "published"
                c["youtube_url"] = f"https://youtube.com/shorts/{vid}"
                changed = True
            elif res.get("status") == "scheduled":
                c["youtube_status"] = "scheduled"
                c["youtube_url"] = f"https://youtube.com/shorts/{vid}"
                changed = True
    if changed:
        save(job)
    return {"ok": True, "job": job}


@app.post("/api/jobs/{jid}/clips/{n}/delete-youtube")
def delete_youtube_clip(jid: str, n: int):
    import youtube_publisher
    if not youtube_publisher.is_connected():
        raise HTTPException(400, "YouTube not connected")
    job = JOBS.get(jid)
    if not job or not (1 <= n <= len(job.get("clips", []))):
        raise HTTPException(404, "Clip not found")
    c = job["clips"][n - 1]
    vid = c.get("youtube_video_id")
    if vid:
        ok = youtube_publisher.delete_video(vid)
        if not ok:
            raise HTTPException(500, "Could not delete from YouTube (video may already be deleted).")
    c["youtube_status"] = "deleted"
    c["youtube_url"] = None
    c["youtube_scheduled"] = None
    c["youtube_video_id"] = None
    save(job)
    return {"ok": True, "message": "Short removed from YouTube schedule."}


@app.post("/api/jobs/{jid}/batch-schedule")
def batch_schedule_job(jid: str, interval_hours: int = Form(2)):
    import youtube_publisher
    if not youtube_publisher.is_connected():
        raise HTTPException(400, "YouTube account is not connected.")
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "Job not found")
    
    now = datetime.datetime.now(datetime.timezone.utc)
    latest_sched = youtube_publisher.get_latest_scheduled_time(JOBS)
    base_time = latest_sched if latest_sched > now else now

    scheduled_count = 0
    step = 0
    for c in job.get("clips", []):
        if c.get("youtube_status") in ("scheduled", "published") and c.get("youtube_url"):
            continue
        best_qual = next((q for q in ("1080p", "720p", "480p") if (jdir(jid) / "clips" / f"clip{c['n']:02d}_{q}.mp4").exists()), None)
        if not best_qual:
            continue
        clip_file = jdir(jid) / "clips" / f"clip{c['n']:02d}_{best_qual}.mp4"
        meta = youtube_publisher.generate_metadata(
            clip_title=c.get("title", f"Clip {c['n']}"),
            clip_text=c.get("text", ""),
            source_title=job.get("title", ""),
            hf_token=job.get("opts", {}).get("hf_token", ""),
            clip_index=c["n"],
        )
        c["youtube_metadata"] = meta
        step += 1
        pub_time = base_time + datetime.timedelta(hours=step * max(1, interval_hours))
        try:
            up_res = youtube_publisher.upload_short(
                video_path=clip_file,
                title=meta["title"],
                description=meta["description"],
                tags=meta["tags"],
                privacy_status="public",
                publish_at=pub_time,
            )
            c["youtube_url"] = up_res["url"]
            c["youtube_video_id"] = up_res["video_id"]
            c["youtube_privacy"] = "public"
            c["youtube_scheduled"] = up_res.get("scheduled")
            c["youtube_status"] = "scheduled"
            scheduled_count += 1
            save(job)
        except Exception as e:
            err_str = str(e)
            if "uploadLimitExceeded" in err_str:
                raise HTTPException(400, "YouTube Daily Upload Limit Reached on your channel. YouTube limits channels to ~10-15 uploads in 24 hours. Please wait until your YouTube quota resets tomorrow.")
            raise HTTPException(500, f"Upload error on clip {c['n']}: {err_str[:200]}")

    return {"ok": True, "scheduled_count": scheduled_count}


# ----------------------------------------------------------------------------- Meta (Instagram & Facebook Reels)
def _meta_callback_uri(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    if "onrender.com" in str(host) or proto == "https":
        return f"https://{host}/api/meta/callback"
    if "127.0.0.1" in str(host) or "localhost" in str(host):
        return "http://localhost:8787/api/meta/callback"
    return f"{str(request.base_url).rstrip('/')}/api/meta/callback"


@app.get("/api/meta/status")
def meta_status():
    import meta_publisher
    return meta_publisher.get_status()


@app.get("/api/meta/auth")
def meta_auth(request: Request):
    import meta_publisher
    cb = _meta_callback_uri(request)
    url = meta_publisher.get_auth_url(cb)
    if not url:
        raise HTTPException(400, "Meta App ID / Secret not configured. Please set META_APP_ID and META_APP_SECRET in .env.")
    return {"url": url}


@app.get("/api/meta/callback")
def meta_callback(code: str, request: Request):
    import meta_publisher
    cb = _meta_callback_uri(request)
    ok, err_msg = meta_publisher.handle_callback(code, cb)
    if not ok:
        return HTMLResponse(f"<body style='font-family:sans-serif;padding:40px;text-align:center;'><h2>❌ Could not connect Meta</h2><p style='color:#c00;'>{err_msg}</p><p>Please check your credentials or try again.</p></body>")
    return HTMLResponse("<body style='font-family:sans-serif;padding:40px;text-align:center;'><h2>✅ Instagram & Facebook Connected!</h2><p>ClipForge is now authorized to publish Reels.</p><p>You can close this tab now.</p><script>setTimeout(() => window.close(), 2500);</script></body>")


@app.post("/api/meta/connect-token")
def meta_connect_token(token: str = Form(...)):
    import meta_publisher
    clean_tok = token.strip()
    if not clean_tok:
        raise HTTPException(400, "Access token is required.")
    ok, msg = meta_publisher.setup_accounts_from_token(clean_tok)
    if not ok:
        raise HTTPException(400, f"Connection failed: {msg}")
    return {"ok": True, "message": msg, "status": meta_publisher.get_status()}


@app.post("/api/meta/disconnect")
def meta_disconnect():
    import meta_publisher
    meta_publisher.disconnect()
    return {"ok": True}


@app.post("/api/jobs/{jid}/clips/{n}/publish-instagram")
def manual_publish_clip_instagram(jid: str, n: int, caption: str = Form("")):
    import meta_publisher
    if not meta_publisher.is_connected() or not meta_publisher.load_token_data().get("instagram_account_id"):
        raise HTTPException(400, "Instagram Business Account is not connected. Connect Meta in ClipForge settings first.")
    job = JOBS.get(jid)
    if not job or not (1 <= n <= len(job.get("clips", []))):
        raise HTTPException(404, "Clip not found")
    c = job["clips"][n - 1]
    best_qual = next((q for q in ("1080p", "720p", "480p") if (jdir(jid) / "clips" / f"clip{n:02d}_{q}.mp4").exists()), None)
    if not best_qual:
        best_qual = "720p"
        render(jid, n, best_qual)
    clip_file = jdir(jid) / "clips" / f"clip{n:02d}_{best_qual}.mp4"
    meta = meta_publisher.generate_reels_metadata(
        clip_title=c.get("title", f"Clip {n}"),
        clip_text=c.get("text", ""),
        source_title=job.get("title", ""),
        clip_index=n,
    )
    final_caption = caption or meta["caption"]
    try:
        res = meta_publisher.upload_instagram_reel(
            video_path=clip_file,
            caption=final_caption,
            share_to_feed=True,
        )
    except Exception as e:
        raise HTTPException(500, f"Instagram upload failed: {str(e)[:250]}")
    c["instagram_url"] = res["url"]
    c["instagram_id"] = res["id"]
    c["instagram_status"] = "published"
    c["instagram_metadata"] = meta
    save(job)
    return res


@app.post("/api/jobs/{jid}/clips/{n}/publish-facebook")
def manual_publish_clip_facebook(jid: str, n: int, title: str = Form(""), description: str = Form("")):
    import meta_publisher
    if not meta_publisher.is_connected() or not meta_publisher.load_token_data().get("page_id"):
        raise HTTPException(400, "Facebook Page is not connected. Connect Meta in ClipForge settings first.")
    job = JOBS.get(jid)
    if not job or not (1 <= n <= len(job.get("clips", []))):
        raise HTTPException(404, "Clip not found")
    c = job["clips"][n - 1]
    best_qual = next((q for q in ("1080p", "720p", "480p") if (jdir(jid) / "clips" / f"clip{n:02d}_{q}.mp4").exists()), None)
    if not best_qual:
        best_qual = "720p"
        render(jid, n, best_qual)
    clip_file = jdir(jid) / "clips" / f"clip{n:02d}_{best_qual}.mp4"
    meta = meta_publisher.generate_reels_metadata(
        clip_title=c.get("title", f"Clip {n}"),
        clip_text=c.get("text", ""),
        source_title=job.get("title", ""),
        clip_index=n,
    )
    final_title = title or meta["title"]
    final_desc = description or meta["caption"]
    try:
        res = meta_publisher.upload_facebook_reel(
            video_path=clip_file,
            title=final_title,
            description=final_desc,
        )
    except Exception as e:
        raise HTTPException(500, f"Facebook Reel upload failed: {str(e)[:250]}")
    c["facebook_url"] = res["url"]
    c["facebook_id"] = res["id"]
    c["facebook_status"] = "published"
    c["facebook_metadata"] = meta
    save(job)
    return res


@app.get("/api/queue")
def get_queue():
    global CURRENT_JOB_ID
    with QUEUE_LOCK:
        cur_job = JOBS.get(CURRENT_JOB_ID) if CURRENT_JOB_ID else None
        queued = []
        for item in JOB_QUEUE:
            qid = item[0]
            qj = JOBS.get(qid, {})
            queued.append({
                "id": qid,
                "title": qj.get("title", item[1] or "Video"),
                "url": item[1],
                "status": qj.get("status", "queued")
            })
        return {
            "current": {"id": CURRENT_JOB_ID, "title": cur_job.get("title", "")} if cur_job else None,
            "queue": queued
        }


@app.post("/api/queue/{jid}/remove")
def remove_from_queue(jid: str):
    global CURRENT_JOB_ID
    with QUEUE_LOCK:
        JOB_QUEUE[:] = [item for item in JOB_QUEUE if item[0] != jid]
    if jid in JOBS:
        JOBS[jid]["status"] = "cancelled"
        JOBS[jid]["message"] = "Removed from queue by user"
        save(JOBS[jid])
    return {"ok": True}


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
