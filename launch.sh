#!/usr/bin/env bash
DIR="/Users/ayush/Downloads/clipforge"
cd "$DIR"

mkdir -p "$DIR/data"

# Always refresh to latest code
lsof -ti:8787 | xargs kill -9 2>/dev/null || true
sleep 0.5

# Launch server in background
nohup "$DIR/.venv/bin/python" "$DIR/server.py" > "$DIR/data/server.log" 2>&1 &

# Wait for server to be responsive
for i in {1..30}; do
    if curl -s http://127.0.0.1:8787/api/destinations > /dev/null 2>&1; then
        break
    fi
    sleep 0.2
done

# Open Web App in browser
open "http://localhost:8787"
