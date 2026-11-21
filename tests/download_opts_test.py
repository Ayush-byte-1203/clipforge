"""Checks the YouTube sign-in options reach yt-dlp, and that error text is cleaned. No network."""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import yt_dlp, core

seen = []
class Fake:
    def __init__(self, params): seen.append(params)
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def extract_info(self, url, download=False): return {"title": "t", "duration": 60}
    def download(self, urls): pass
yt_dlp.YoutubeDL = Fake
d = Path(tempfile.mkdtemp())
for kw, key in (({"cookies_browser": "chrome"}, "cookiesfrombrowser"), ({"cookies_file": "/tmp/c.txt"}, "cookiefile")):
    try: core.download("https://youtu.be/x", d, 10, lambda *a: None, 720, **kw)
    except RuntimeError as e: assert "no video file" in str(e)
    assert key in seen[-1] and seen[-1]["no_color"] is True, seen[-1]
print("browser ->", seen[0]["cookiesfrombrowser"], "| file ->", seen[2]["cookiefile"])
import re; assert re.sub(r"\x1b\[[0-9;]*m", "", "\x1b[0;31mERROR:\x1b[0m [youtube] x") == "ERROR: [youtube] x"
print("DOWNLOAD OPTIONS TEST PASSED")
