#!/usr/bin/env bash
# ==============================================================================
# ClipForge Vercel Build Script
# ==============================================================================
set -e

echo "==> [Vercel Build] Upgrading pip..."
python3 -m pip install --upgrade pip

echo "==> [Vercel Build] Installing dependencies from requirements.txt..."
pip install -r requirements.txt

echo "==> [Vercel Build] Verifying FastAPI & Uvicorn..."
python3 -c "import fastapi, uvicorn; print('FastAPI & Uvicorn verified')"

echo "==> [Vercel Build] Preparing temporary directories..."
mkdir -p /tmp/clipforge_data/jobs

echo "==> [Vercel Build] Build completed successfully!"
