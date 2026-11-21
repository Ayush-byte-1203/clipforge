"""ClipForge – YouTube Shorts Auto-Publisher Integration.

Handles OAuth2 authentication and video uploading to YouTube Data API v3.
Includes automated viral metadata generator (titles, descriptions, hashtags, tags).
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("CLIPFORGE_DATA", ROOT / "data")).resolve()

# Allow HTTP transport for local/reverse-proxy OAuth flow
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
TOKEN_PATH = DATA / "youtube_token.json"

log = logging.getLogger("clipforge.youtube")


# ═══════════════════════════════════════════════════════════════════ helpers ══


def _get_client_id_secret() -> tuple[str, str]:
    cid = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    sec = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    if cid and sec:
        return cid, sec
    # Check .env directly
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("GOOGLE_CLIENT_ID="):
                cid = line.split("=", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("GOOGLE_CLIENT_SECRET="):
                sec = line.split("=", 1)[1].strip().strip('"').strip("'")
    if cid and sec:
        os.environ["GOOGLE_CLIENT_ID"] = cid
        os.environ["GOOGLE_CLIENT_SECRET"] = sec
        return cid, sec
    # Fallback to drive_token.json where Drive saved client_id/secret
    dt = DATA / "drive_token.json"
    if dt.exists():
        try:
            data = json.loads(dt.read_text(encoding="utf-8"))
            cid = data.get("client_id", "").strip()
            sec = data.get("client_secret", "").strip()
            if cid and sec:
                os.environ["GOOGLE_CLIENT_ID"] = cid
                os.environ["GOOGLE_CLIENT_SECRET"] = sec
                return cid, sec
        except Exception:
            pass
    return cid, sec


def is_configured() -> bool:
    cid, sec = _get_client_id_secret()
    return bool(cid and sec)


def is_connected() -> bool:
    if not TOKEN_PATH.exists():
        return False
    try:
        creds = _load_credentials()
        return creds is not None and creds.valid
    except Exception:
        return False


def _load_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN_PATH.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    except Exception:
        log.warning("youtube_token.json is corrupt")
        return None

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        except Exception as exc:
            log.warning("YouTube Token refresh failed: %s", exc)
            return None

    return creds if creds.valid else None


def _build_service():
    from googleapiclient.discovery import build

    creds = _load_credentials()
    if creds is None:
        raise RuntimeError("YouTube account is not connected. Please authenticate first.")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


# ═══════════════════════════════════════════════════════════════════ auth ══════


def get_auth_url(redirect_uri: str) -> str:
    client_id, client_secret = _get_client_id_secret()
    if not client_id or not client_secret:
        raise RuntimeError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars are not set.")

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"


def handle_callback(code: str, redirect_uri: str) -> tuple[bool, str]:
    from google.oauth2.credentials import Credentials

    client_id, client_secret = _get_client_id_secret()
    if not client_id or not client_secret:
        return False, "Google credentials missing."

    data = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            token_data = json.loads(resp.read().decode("utf-8"))

        creds = Credentials(
            token=token_data.get("access_token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        return True, ""
    except Exception as exc:
        msg = str(exc)
        if hasattr(exc, "read"):
            try:
                msg = exc.read().decode("utf-8")
            except Exception:
                pass
        return False, msg


def disconnect() -> bool:
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()
        return True
    return False


def get_channel_info() -> dict[str, str] | None:
    if not is_connected():
        return None
    try:
        youtube = _build_service()
        res = youtube.channels().list(part="snippet", mine=True).execute()
        items = res.get("items", [])
        if items:
            snip = items[0]["snippet"]
            return {
                "id": items[0]["id"],
                "title": snip.get("title", "My Channel"),
                "thumbnail": snip.get("thumbnails", {}).get("default", {}).get("url", ""),
            }
    except Exception as e:
        log.warning("Could not fetch channel info: %s", e)
    return None


def get_status() -> dict[str, Any]:
    configured = is_configured()
    connected = is_connected()
    channel = get_channel_info() if connected else None
    return {
        "configured": configured,
        "connected": connected,
        "channel": channel,
    }


# ══════════════════════════════════════════════════ viral metadata generator ══


def generate_metadata(clip_title: str, clip_text: str, source_title: str, hf_token: str = "") -> dict[str, Any]:
    """Auto-generate viral title, SEO description, hashtags, and keyword tags for YouTube Shorts."""
    # Heuristic topic detection
    text_lower = (clip_text + " " + source_title).lower()
    topic = "gaming"
    for cand in ("gta", "minecraft", "cod", "roblox", "fortnite", "valorant", "streamer", "react", "funny"):
        if cand in text_lower:
            topic = cand
            break

    # Extract punchline words or key moment
    sentences = re.split(r"[.!?]+", clip_text)
    punchline = sentences[0].strip() if sentences else clip_title
    if len(punchline) > 50:
        punchline = punchline[:47] + "..."

    # 1. Viral Title (Must include #Shorts, emojis, and a curiosity hook)
    clean_title = clip_title.strip()
    if clean_title:
        title = f"{clean_title.upper()}! 😱 #Shorts"
    else:
        title = f"{punchline.upper()} 💀 #Shorts"

    if len(title) > 95:
        title = title[:85].rstrip() + " 😱 #Shorts"

    # 2. Viral Hashtags
    hashtags = ["#Shorts", "#YouTubeShorts", "#Viral", "#Trending"]
    if topic != "gaming":
        hashtags.append(f"#{topic.capitalize()}")
    else:
        hashtags.extend(["#Gaming", "#Gamers", "#GamingClips"])

    # 3. SEO Description
    desc_lines = [
        f"{clip_title or punchline}",
        "",
        f"Moment from: {source_title}",
        "",
        "Like and Subscribe for daily gaming moments and clips! 🔔",
        "",
        " ".join(hashtags),
    ]
    description = "\n".join(desc_lines)

    # 4. Search Tags
    tags = [
        "shorts",
        "youtube shorts",
        "viral clips",
        "trending",
        "viral",
        "best moments",
        "funny moments",
        "highlights",
    ]
    if topic:
        tags.extend([topic, f"{topic} clips", f"{topic} funny", f"{topic} shorts"])
    # Add key words from transcript
    words = [w.lower() for w in re.findall(r"\b[A-Za-z]{4,}\b", clip_text)]
    common = ("that", "this", "what", "with", "from", "have", "here", "know", "just", "like", "going", "about")
    unique_words = [w for w in set(words) if w not in common][:6]
    tags.extend(unique_words)

    return {
        "title": title,
        "description": description,
        "tags": tags[:15],
        "hashtags": hashtags,
    }


# ══════════════════════════════════════════════════════════ upload to shorts ══


def upload_short(
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    privacy_status: str = "public",
    publish_at: datetime.datetime | None = None,
) -> dict[str, Any]:
    """Upload a 9:16 video to YouTube as a Short.
    
    privacy_status: "public", "private", or "unlisted"
    publish_at: optional datetime for scheduled release (must be future, privacy_status becomes private until then)
    """
    from googleapiclient.http import MediaFileUpload

    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    # Ensure #Shorts is present in title or description
    if "#shorts" not in title.lower():
        title = f"{title} #Shorts"[:100]

    youtube = _build_service()

    status_body: dict[str, Any] = {
        "privacyStatus": privacy_status,
        "selfDeclaredMadeForKids": False,
    }

    if publish_at is not None:
        # Scheduled publishing requires privacyStatus to be "private"
        status_body["privacyStatus"] = "private"
        status_body["publishAt"] = publish_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags[:30],
            "categoryId": "20",  # Gaming
        },
        "status": status_body,
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=10 * 1024 * 1024,
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info("Uploaded %d%%", int(status.progress() * 100))

    video_id = response.get("id")
    shorts_url = f"https://youtube.com/shorts/{video_id}"
    log.info("YouTube Short published successfully: %s", shorts_url)

    return {
        "video_id": video_id,
        "url": shorts_url,
        "title": title,
        "privacy": privacy_status,
        "scheduled": publish_at.isoformat() if publish_at else None,
    }
