"""Vercel Serverless Function entrypoint for ClipForge FastAPI application."""
import os
import sys
from pathlib import Path
from fastapi import Request
from fastapi.responses import HTMLResponse

# Ensure project root is in sys.path
ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Vercel serverless environment has a read-only filesystem except for /tmp
os.environ.setdefault("VERCEL", "1")
os.environ.setdefault("CLIPFORGE_DATA", "/tmp/clipforge_data")

# Ensure temporary working directories exist
Path(os.environ["CLIPFORGE_DATA"]).mkdir(parents=True, exist_ok=True)
(Path(os.environ["CLIPFORGE_DATA"]) / "jobs").mkdir(parents=True, exist_ok=True)

# Import FastAPI app from server
from server import app

# Normalise Vercel serverless routing paths so root and API endpoints resolve seamlessly
@app.middleware("http")
async def vercel_routing_middleware(request: Request, call_next):
    matched = request.headers.get("x-matched-path")
    if matched:
        request.scope["path"] = matched
    elif request.scope["path"] in ("/api/index.py", "/api/index", "/api"):
        request.scope["path"] = "/"
    elif request.scope["path"].startswith("/api/index.py/"):
        request.scope["path"] = request.scope["path"][len("/api/index.py"):]
    elif request.scope["path"].startswith("/api/index/"):
        request.scope["path"] = request.scope["path"][len("/api/index"):]
    return await call_next(request)

@app.get("/api/index.py", response_class=HTMLResponse)
@app.get("/api/index", response_class=HTMLResponse)
@app.get("/api", response_class=HTMLResponse)
def vercel_root():
    return (ROOT / "index.html").read_text(encoding="utf-8")
