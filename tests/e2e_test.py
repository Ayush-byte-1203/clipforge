"""End-to-end test with a synthetic video + stub transcript (no internet, no model download needed).
Run:  python tests/e2e_test.py
It exercises the real server: upload -> analyse -> render -> download at 3 qualities."""
import json, os, subprocess, sys, tempfile, threading, time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
tmp = Path(tempfile.mkdtemp(prefix="clipforge_test_"))
os.environ["CLIPFORGE_DATA"] = str(tmp / "data")

import cv2, httpx, uvicorn
from skimage import data as skdata
import core, server

# ---- 1. synthetic source video: a face that jumps left -> right, audio gets loud in bursts
face = cv2.cvtColor(skdata.astronaut(), cv2.COLOR_RGB2BGR)
cv2.imwrite(str(tmp / "face.png"), face)
vid = tmp / "test_talk.mp4"
DUR = 180
subprocess.run([core.ffmpeg_bin(), "-y", "-v", "error",
    "-f", "lavfi", "-i", f"color=c=0x203040:s=1280x720:r=30:d={DUR}",
    "-loop", "1", "-i", str(tmp / "face.png"),
    "-f", "lavfi", "-i", f"aevalsrc=0.5*sin(2*PI*220*t)*(0.12+0.88*lt(mod(t\\,60)\\,25)):s=16000:d={DUR}",
    "-filter_complex", "[1:v]scale=420:420[a];[0:v][a]overlay=x='if(lt(t,60),100,if(lt(t,120),780,440))':y=150:shortest=1[v]",
    "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", str(DUR), str(vid)],
    check=True)

# ---- 2. stub transcript (stands in for Whisper): hooky sentences sit inside the loud bursts
HOOKY = ["Why does nobody talk about this huge mistake?", "This is the biggest secret and it's insane!",
         "Stop doing this right now, it will ruin everything!", "How I made a million dollars in one year."]
BORING = ["So yeah we went to the store and picked up some things.", "And then we sort of looked at the schedule for next week.",
          "It was a fairly normal day with nothing much happening at all."]
words, t, k = [], 1.0, 0
while t < DUR - 4:
    loud = (t % 60) < 25
    s = (HOOKY if loud else BORING)[k % (4 if loud else 3)]
    k += 1
    for w in s.split():
        words.append({"w": w, "s": round(t, 2), "e": round(t + 0.32, 2)}); t += 0.38
    t += 0.5
core.transcribe = lambda wav, model, lang, dur, report: {"language": "en", "words": words}

# ---- 3. start the real server
cfg = uvicorn.Config(server.app, host="127.0.0.1", port=8799, log_level="error")
srv = uvicorn.Server(cfg); threading.Thread(target=srv.run, daemon=True).start()
time.sleep(1.5)
B = "http://127.0.0.1:8799"
c = httpx.Client(timeout=600)
assert "ClipForge" in c.get(B + "/").text

with open(vid, "rb") as f:
    r = c.post(B + "/api/jobs", data={"num_clips": 5, "min_len": 20, "max_len": 45, "layout": "fill", "delete_source": "false", "qualities": "720p"},
               files={"file": ("test_talk.mp4", f, "video/mp4")})
r.raise_for_status(); jid = r.json()["id"]
t0 = time.time()
while True:
    j = c.get(f"{B}/api/jobs/{jid}").json()
    if j["status"] in ("done", "error"): break
    time.sleep(1)
print(f"job {j['status']} in {time.time()-t0:.0f}s:", j.get("error") or j["message"])
assert j["status"] == "done", j.get("error")
print("notes:", j["notes"])
for cl in j["clips"]:
    print(f"  clip {cl['n']}: {cl['start']:6.1f}-{cl['end']:6.1f}s score {cl['score']:5.1f}  '{cl['title']}'  {cl['parts']}")
assert 1 <= len(j["clips"]) <= 5
starts = sorted((x["start"], x["end"]) for x in j["clips"])
assert all(a[1] <= b[0] + 6 for a, b in zip(starts, starts[1:])), "clips overlap"
loud_hits = sum((x["start"] % 60) < 30 for x in j["clips"])
print("clips starting inside loud/hooky sections:", loud_hits, "of", len(j["clips"]))
assert loud_hits >= len(j["clips"]) - 1

# ---- 4. downloads in every quality, check real output dimensions
for q, (w, h, _) in core.QUALITIES.items():
    r = c.get(f"{B}/api/jobs/{jid}/clips/1/video", params={"quality": q, "download": "true"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"], r.text[:200]
    p = tmp / f"out_{q}.mp4"; p.write_bytes(r.content)
    pi = core.probe(p)
    print(f"  {q}: {pi['width']}x{pi['height']} {pi['duration']:.1f}s {len(r.content)//1024} KB audio={pi['has_audio']}")
    assert (pi["width"], pi["height"]) == (w, h) and pi["has_audio"]
# face tracking keys
keys = json.loads((tmp / "data/jobs" / jid / "clips/work01/crop.json").read_text())
print("crop keyframes (t, x):", [[round(a, 1), round(b)] for a, b in keys][:8])
# fit layout also works
r = c.post(B + "/api/jobs", data={"url": str(vid), "num_clips": 2, "layout": "fit", "captions": "false", "hook": "false"})
jid2 = r.json()["id"]
while (j2 := c.get(f"{B}/api/jobs/{jid2}").json())["status"] not in ("done", "error"): time.sleep(1)
assert j2["status"] == "done", j2.get("error")
print("fit layout ok:", len(j2["clips"]), "clips")
# bad input is handled
assert c.post(B + "/api/jobs", data={"url": "not a thing"}).status_code == 400

# ---- 5. storage features: save to folder, delete source, quality selection
def run_job(**data):
    jid = c.post(B + "/api/jobs", data={"url": str(vid), "num_clips": 2, **data}).json()["id"]
    while (jj := c.get(f"{B}/api/jobs/{jid}").json())["status"] not in ("done", "error"): time.sleep(1)
    assert jj["status"] == "done", jj.get("error")
    return jid, jj
drive = tmp / "drive"
jid3, j3 = run_job(qualities="480p,720p", delete_source="true", save_to=str(drive))
d3 = tmp / "data/jobs" / jid3
assert not list(d3.glob("source.*")) and not (d3 / "audio.wav").exists(), "source not deleted"
saved = sorted(p.name for p in Path(j3["saved_to"]).iterdir())
print("saved to folder:", saved[:2], "... total", len(saved))
assert len(saved) == len(j3["clips"]) * 2 and all(n.endswith(("_480p.mp4", "_720p.mp4")) for n in saved)
assert j3["clips"][0]["available"] == ["480p", "720p"], j3["clips"][0]["available"]
assert c.get(f"{B}/api/jobs/{jid3}/clips/1/video", params={"quality": "1080p"}).status_code == 410
assert c.get(f"{B}/api/jobs/{jid3}/clips/1/video", params={"quality": "720p"}).status_code == 200
print("job size on disk after cleanup:", j3["size_mb"], "MB")

# fake rclone (stand-in for Google Drive) + remove local copies after upload
bindir = tmp / "bin"; bindir.mkdir(); cloud = tmp / "cloud"
(bindir / "rclone").write_text(f"#!/bin/sh\n[ \"$1\" = copyto ] || exit 2\ndst=\"{cloud}/${{3#*:}}\"\nmkdir -p \"$(dirname \"$dst\")\"\ncp \"$2\" \"$dst\"\n")
(bindir / "rclone").chmod(0o755); os.environ["PATH"] = f"{bindir}:{os.environ['PATH']}"
jid4, j4 = run_job(qualities="720p", delete_source="true", save_to="gdrive:ClipForge", remove_local="true")
up = list(cloud.rglob("*.mp4")); print("uploaded via rclone:", len(up), "files; local_deleted =", j4.get("local_deleted"))
assert len(up) == len(j4["clips"]) and j4["local_deleted"] and not list((tmp / "data/jobs" / jid4).rglob("clip*.mp4"))
assert c.get(f"{B}/api/jobs/{jid4}/clips/1/video").status_code == 410
# failed upload must never delete anything
jid5, j5 = run_job(qualities="720p", save_to="nope:Folder", remove_local="true")
(bindir / "rclone").write_text("#!/bin/sh\necho boom >&2\nexit 1\n")
jid5, j5 = run_job(qualities="720p", save_to="nope:Folder", remove_local="true")
assert j5.get("save_error") and not j5.get("local_deleted") and c.get(f"{B}/api/jobs/{jid5}/clips/1/video").status_code == 200
print("failed upload keeps local files:", j5["save_error"][:40])
assert c.delete(f"{B}/api/jobs/{jid5}").json()["ok"] and not (tmp / "data/jobs" / jid5).exists()
print("frame samples saved in", tmp)
print("ALL TESTS PASSED")
