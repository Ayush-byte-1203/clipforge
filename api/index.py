"""Vercel Serverless Function entrypoint for ClipForge FastAPI application."""
import os
import sys
from pathlib import Path

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
