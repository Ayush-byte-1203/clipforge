#!/usr/bin/env bash
set -e

echo "=========================================================="
echo "  ClipForge 1-Click Free Cloud Setup (Oracle Free Tier)   "
echo "=========================================================="

# 1. Install Docker & curl if missing
if ! command -v docker &>/dev/null; then
    echo "--> [1/4] Installing Docker engine..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER" || true
else
    echo "--> [1/4] Docker already installed."
fi

# 2. Make sure .env exists
if [ ! -f .env ]; then
    echo "--> [2/4] Creating initial .env configuration..."
    cat > .env <<'EOF'
# Google Drive API credentials (optional, can be added anytime)
# GOOGLE_CLIENT_ID=
# GOOGLE_CLIENT_SECRET=
EOF
fi

# 3. Build container
echo "--> [3/4] Building ClipForge container (takes ~2 mins)..."
sudo docker build -t clipforge .

# 4. Run container
echo "--> [4/4] Starting ClipForge server..."
sudo docker rm -f clipforge 2>/dev/null || true
sudo docker run -d \
    --name clipforge \
    --restart always \
    -p 80:8787 \
    -v clipforge_data:/data \
    --env-file .env \
    clipforge

SERVER_IP=$(curl -s -4 ifconfig.me 2>/dev/null || curl -s -4 icanhazip.com 2>/dev/null || echo "YOUR_SERVER_IP")

echo ""
echo "=========================================================="
echo "  🎉 ClipForge is now permanently running online!"
echo "  Access anytime from your phone/laptop at:"
echo "  http://${SERVER_IP}"
echo "=========================================================="
