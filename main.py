"""Root ASGI entrypoint for ClipForge (FastAPI)."""
from server import app

if __name__ == "__main__":
    import os
    import uvicorn
    host = os.environ.get("CLIPFORGE_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("CLIPFORGE_PORT", 8787)))
    uvicorn.run("main:app", host=host, port=port, reload=False)
