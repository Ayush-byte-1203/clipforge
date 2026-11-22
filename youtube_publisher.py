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
    cid = (os.environ.get("GOOGLE_CLIENT_ID") or os.environ.get("CLIENT_ID") or "").strip()
    sec = (os.environ.get("GOOGLE_CLIENT_SECRET") or os.environ.get("CLIENT_SECRET") or "").strip()
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
        "prompt": "select_account consent",
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


def check_video_status(video_id: str) -> dict[str, Any]:
    """Check if a video still exists on YouTube, or if it has been deleted / changed privacy."""
    if not is_connected() or not video_id:
        return {"exists": False, "status": "unknown"}
    try:
        youtube = _build_service()
        res = youtube.videos().list(part="status,snippet", id=video_id).execute()
        items = res.get("items", [])
        if not items:
            return {"exists": False, "status": "deleted"}
        st = items[0].get("status", {})
        privacy = st.get("privacyStatus", "public")
        publish_at = st.get("publishAt")
        upload_status = st.get("uploadStatus", "processed")
        if upload_status == "rejected":
            return {"exists": False, "status": "rejected"}
        if publish_at:
            return {"exists": True, "status": "scheduled", "privacy": privacy, "publish_at": publish_at}
        return {"exists": True, "status": "live", "privacy": privacy}
    except Exception as e:
        log.warning("Could not check video %s status: %s", video_id, e)
        return {"exists": True, "status": "unknown"}


def delete_video(video_id: str) -> bool:
    """Delete a video or scheduled Short from YouTube."""
    if not is_connected() or not video_id:
        return False
    try:
        youtube = _build_service()
        youtube.videos().delete(id=video_id).execute()
        log.info("Deleted YouTube video %s successfully", video_id)
        return True
    except Exception as e:
        log.warning("Could not delete video %s from YouTube: %s", video_id, e)
        return False


def get_latest_scheduled_time(jobs_dict: dict | None = None) -> datetime.datetime:
    """Find the latest scheduled YouTube publishing time across all existing jobs and live channel uploads.
    Ensures newly scheduled clips chain seamlessly after previous clips with zero clashes.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    latest = now
    # 1. Check in-memory / local jobs
    if jobs_dict:
        for j in jobs_dict.values():
            for c in j.get("clips", []):
                sched_str = c.get("youtube_scheduled")
                if sched_str and c.get("youtube_status") == "scheduled":
                    try:
                        clean_str = sched_str.replace("Z", "+00:00")
                        dt = datetime.datetime.fromisoformat(clean_str)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=datetime.timezone.utc)
                        if dt > latest:
                            latest = dt
                    except Exception:
                        pass
    # 2. Check live YouTube channel scheduled uploads
    try:
        if is_connected():
            youtube = _build_service()
            ch_res = youtube.channels().list(part="contentDetails", mine=True).execute()
            items = ch_res.get("items", [])
            if items:
                uploads_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
                pl_res = youtube.playlistItems().list(part="contentDetails", playlistId=uploads_id, maxResults=20).execute()
                vids = [it["contentDetails"]["videoId"] for it in pl_res.get("items", [])]
                if vids:
                    v_res = youtube.videos().list(part="status", id=",".join(vids)).execute()
                    for v in v_res.get("items", []):
                        pub_at = v.get("status", {}).get("publishAt")
                        if pub_at:
                            clean_str = pub_at.replace("Z", "+00:00")
                            dt = datetime.datetime.fromisoformat(clean_str)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=datetime.timezone.utc)
                            if dt > latest:
                                latest = dt
    except Exception as e:
        log.warning("Could not query live YouTube uploads for latest schedule: %s", e)
    return latest


# ══════════════════════════════════════════════════ viral metadata generator ══


EMOJIS_LIST = ["🔥", "💀", "😂", "🤯", "🚨", "🎯", "⚡️", "👀", "💥", "🤣", "😱", "🏆"]


def _clean_title_case(s: str) -> str:
    """Format string into clean, natural title case instead of repetitive all-caps."""
    s = s.strip().rstrip(".!?,")
    if not s:
        return ""
    words = s.split()
    lower_words = {"a", "an", "the", "and", "but", "or", "for", "nor", "on", "at", "to", "from", "by", "over", "in", "of"}
    out = []
    for i, w in enumerate(words):
        if i == 0 or i == len(words) - 1 or w.lower() not in lower_words:
            out.append(w.capitalize())
        else:
            out.append(w.lower())
    return " ".join(out)


def generate_metadata(
    clip_title: str,
    clip_text: str,
    source_title: str,
    hf_token: str = "",
    clip_index: int = 1,
) -> dict[str, Any]:
    """Auto-generate viral title, rich SEO description, hashtags, and keyword tags for YouTube Shorts.
    - Contextual, varied emojis per clip (no repetitive emojis!)
    - Smart Title Case hooks with game series context
    - Comprehensive long description with engagement CTA
    - Packs keyword tags up to YouTube's 500-character limit."""
    combined_text = (clip_text + " " + source_title).lower()

    # 1. Topic & Game detection
    topic = "gaming"
    for cand in ("gta 6", "gta vi", "gta 5", "gta v", "gta", "minecraft", "cod", "warzone", "roblox",
                 "fortnite", "valorant", "streamer", "react", "funny", "pubg", "apex", "csgo", "fifa"):
        if cand in combined_text:
            topic = cand
            break

    # 2. Episode / Series detection (e.g. #3, Episode 3, Part 2)
    ep_num = ""
    ep_match = re.search(r"(?:episode|ep|part)\s*#?(\d+)|#(\d+)", source_title + " " + clip_title, re.I)
    if ep_match:
        ep_num = ep_match.group(1) or ep_match.group(2)

    # 3. Dynamic Emoji Selection (Contextual & Rotated per clip)
    text_sample = (clip_title + " " + clip_text[:120]).lower()
    if any(k in text_sample for k in ("dead", "died", "ruined", "scared", "fear", "lost", "kill")):
        emoji = "💀"
    elif any(k in text_sample for k in ("funny", "laugh", "joke", "hilarious", "why not", "wtf")):
        emoji = "😂" if clip_index % 2 == 1 else "🤣"
    elif any(k in text_sample for k in ("clutch", "win", "jump", "insane", "speed", "fast")):
        emoji = "🔥" if clip_index % 2 == 1 else "⚡️"
    elif any(k in text_sample for k in ("omg", "no way", "impossible", "look", "secret")):
        emoji = "🤯" if clip_index % 2 == 1 else "👀"
    else:
        emoji = EMOJIS_LIST[(clip_index - 1) % len(EMOJIS_LIST)]

    # 4. Punchline & Viral Title Formulation
    sentences = [s.strip() for s in re.split(r"[.!?]+", clip_text) if s.strip()]
    punchline = sentences[0] if sentences else clip_title
    if len(punchline) > 55:
        punchline = punchline[:52] + "..."

    clean_base = _clean_title_case(clip_title or punchline)

    # Add game label if relevant and fits
    game_tag_name = "GTA 6" if "gta" in topic else topic.capitalize()
    if game_tag_name.lower() not in clean_base.lower() and len(clean_base) + len(game_tag_name) < 65:
        title_core = f"{clean_base} ({game_tag_name})"
    else:
        title_core = clean_base

    title = f"{title_core} {emoji} #Shorts"
    if len(title) > 95:
        title = f"{clean_base[:70].rstrip()} {emoji} #Shorts"

    # 5. Viral Hashtags
    hashtags = ["#Shorts", "#YouTubeShorts", "#Viral", "#Trending"]
    if "gta" in topic:
        hashtags.extend(["#GTA6", "#GTAV", "#Gaming", "#GamingClips"])
    elif topic != "gaming":
        hashtags.extend([f"#{topic.replace(' ', '').capitalize()}", "#Gaming", "#Gamers"])
    else:
        hashtags.extend(["#Gaming", "#Gamers", "#GamingClips", "#Clutch"])

    if ep_num:
        hashtags.append(f"#Episode{ep_num}")

    # 6. Rich Long SEO Description
    desc_lines = [
        f"{title_core} {emoji}",
        "",
        f"👉 Moment: {punchline}",
        "Tell us in the comments: What would you do in this situation? Drop a comment below! 👇",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🎬 Original Video: {source_title or 'Gaming Stream Highlight'}",
        f"🎮 Featured Game: {game_tag_name}",
        f"✂️ Clip Moment: #{clip_index}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "🔔 SUBSCRIBE for daily gaming moments, funny highlights & insane clutches!",
        "👍 Leave a LIKE if you enjoyed this Short!",
        "🔄 SHARE this with your squad!",
        "",
        " ".join(hashtags),
    ]
    description = "\n".join(desc_lines)

    # 7. Comprehensive Keyword Tags (Up to 485 characters under 500-char limit)
    candidates = [
        "shorts",
        "youtube shorts",
        "viral clips",
        "trending",
        "viral",
        "best moments",
        "funny moments",
        "epic moments",
        "gaming clips",
        "gaming shorts",
        "streamer moments",
        "twitch highlights",
        "clutch moments",
        "insane plays",
        "trending shorts",
        "foryou",
        "fyp",
        "daily shorts",
    ]

    if "gta" in topic:
        candidates.extend([
            "gta 6", "gta 6 gameplay", "gta vi", "gta 6 shorts", "gta 6 leak",
            "gta 6 funny moments", "gta roleplay", "grand theft auto 6", "gta v",
            "gta gameplay", "gta highlights", "gta fails"
        ])
    elif topic != "gaming":
        clean_topic = topic.lower()
        candidates.extend([
            clean_topic,
            f"{clean_topic} shorts",
            f"{clean_topic} funny moments",
            f"{clean_topic} highlights",
            f"{clean_topic} gameplay",
            f"{clean_topic} best clips",
            f"{clean_topic} viral",
        ])

    if ep_num:
        candidates.extend([f"episode {ep_num}", f"gta episode {ep_num}", f"part {ep_num}"])

    # Salient transcript and title terms
    words_raw = re.findall(r"\b[A-Za-z]{4,}\b", clip_text + " " + source_title + " " + clip_title)
    stop_words = {"that", "this", "what", "with", "from", "have", "here", "know", "just", "like", "going", "about",
                  "your", "them", "then", "there", "they", "some", "when", "would", "could", "should", "want", "more",
                  "video", "source", "clips"}
    for w in words_raw:
        wl = w.lower()
        if wl not in stop_words and wl not in candidates:
            candidates.append(wl)

    # Multi-word title phrases
    tokens = clean_base.split()
    for i in range(len(tokens) - 1):
        phrase = f"{tokens[i]} {tokens[i+1]}".lower()
        if phrase not in candidates and len(phrase) > 5:
            candidates.append(phrase)

    # Pack keywords up to 485 characters
    final_tags = []
    total_chars = 0
    for cand in candidates:
        cand_clean = cand.strip().lower()
        if not cand_clean or cand_clean in final_tags:
            continue
        cost = len(cand_clean) + 1  # includes comma
        if total_chars + cost <= 485:
            final_tags.append(cand_clean)
            total_chars += cost

    return {
        "title": title,
        "description": description,
        "tags": final_tags,
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
