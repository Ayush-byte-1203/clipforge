"""ClipForge – Instagram & Facebook Reels Auto-Publisher Integration.

Handles Meta Graph API authentication (OAuth2 and Direct Token),
automated viral Reels metadata generation, and direct binary uploading
via Meta's Resumable Upload protocol for Instagram Reels and Facebook Reels.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("CLIPFORGE_DATA", ROOT / "data")).resolve()
TOKEN_PATH = DATA / "meta_token.json"

GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

SCOPES = [
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "instagram_basic",
    "instagram_content_publish",
    "business_management",
]

log = logging.getLogger("clipforge.meta")


# ═══════════════════════════════════════════════════════════════════ config ═══


def _get_app_credentials() -> tuple[str, str]:
    """Retrieve Meta App ID and Secret from environment or .env file."""
    app_id = os.environ.get("META_APP_ID", "").strip()
    app_sec = os.environ.get("META_APP_SECRET", "").strip()
    if app_id and app_sec:
        return app_id, app_sec

    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("META_APP_ID="):
                app_id = line.split("=", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("META_APP_SECRET="):
                app_sec = line.split("=", 1)[1].strip().strip('"').strip("'")
    if app_id and app_sec:
        os.environ["META_APP_ID"] = app_id
        os.environ["META_APP_SECRET"] = app_sec
        return app_id, app_sec

    # Check meta_token.json
    if TOKEN_PATH.exists():
        try:
            data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
            app_id = data.get("app_id", "").strip()
            app_sec = data.get("app_secret", "").strip()
            if app_id and app_sec:
                return app_id, app_sec
        except Exception:
            pass

    return app_id, app_sec


def load_token_data() -> dict[str, Any]:
    """Load stored Meta connection data."""
    if not TOKEN_PATH.exists():
        return {}
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.warning("meta_token.json is corrupted")
        return {}


def save_token_data(data: dict[str, Any]):
    """Persist Meta connection state."""
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = load_token_data()
    existing.update(data)
    TOKEN_PATH.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def is_configured() -> bool:
    """True if Meta App credentials or an access token are available."""
    app_id, app_sec = _get_app_credentials()
    if app_id and app_sec:
        return True
    tok = load_token_data()
    return bool(tok.get("access_token") or tok.get("page_access_token"))


def is_connected() -> bool:
    """True if active credentials and at least one target (Instagram or Facebook) are connected."""
    data = load_token_data()
    token = data.get("page_access_token") or data.get("access_token")
    if not token:
        return False
    return bool(data.get("instagram_account_id") or data.get("page_id"))


def disconnect() -> bool:
    """Disconnect Meta account by removing stored tokens."""
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()
        return True
    return False


def get_status() -> dict[str, Any]:
    """Return status of Meta (Instagram & Facebook) connections."""
    data = load_token_data()
    configured = is_configured()
    connected = is_connected()

    ig_info = None
    if data.get("instagram_account_id"):
        ig_info = {
            "id": data.get("instagram_account_id"),
            "username": data.get("instagram_username", "Instagram Creator"),
            "profile_pic": data.get("instagram_profile_pic", ""),
        }

    fb_info = None
    if data.get("page_id"):
        fb_info = {
            "id": data.get("page_id"),
            "name": data.get("page_name", "Facebook Page"),
        }

    return {
        "configured": configured,
        "connected": connected,
        "instagram": ig_info,
        "facebook": fb_info,
    }


# ═══════════════════════════════════════════════════════════════════ auth ══════


def get_auth_url(redirect_uri: str) -> str:
    """Generate Facebook / Instagram OAuth authorization URL."""
    app_id, _ = _get_app_credentials()
    if not app_id:
        raise RuntimeError("META_APP_ID is not configured. Please set it in .env or the UI.")

    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": ",".join(SCOPES),
        "response_type": "code",
    }
    return f"https://www.facebook.com/{GRAPH_API_VERSION}/dialog/oauth?{urllib.parse.urlencode(params)}"


def handle_callback(code: str, redirect_uri: str) -> tuple[bool, str]:
    """Exchange authorization code for long-lived user token and discover pages & IG accounts."""
    app_id, app_secret = _get_app_credentials()
    if not app_id or not app_secret:
        return False, "Meta App ID / App Secret is missing."

    try:
        # 1. Exchange code for short-lived user token
        resp = requests.get(
            f"{GRAPH_API_BASE}/oauth/access_token",
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            err = resp.json().get("error", {}).get("message", resp.text)
            return False, f"Token exchange failed: {err}"
        short_token = resp.json().get("access_token")

        # 2. Exchange for 60-day long-lived user token
        ll_resp = requests.get(
            f"{GRAPH_API_BASE}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": short_token,
            },
            timeout=15,
        )
        user_token = ll_resp.json().get("access_token", short_token)

        # 3. Discover linked Facebook Pages and Instagram Business Accounts
        ok, msg = setup_accounts_from_token(user_token)
        if not ok:
            return False, msg

        save_token_data({"app_id": app_id, "app_secret": app_secret, "access_token": user_token})
        return True, ""
    except Exception as exc:
        log.exception("Meta OAuth callback exception")
        return False, str(exc)


def setup_accounts_from_token(token: str) -> tuple[bool, str]:
    """Inspect token, query linked Pages and Instagram Business accounts, and persist."""
    try:
        url = f"{GRAPH_API_BASE}/me/accounts"
        params = {
            "fields": "id,name,access_token,instagram_business_account{id,username,profile_picture_url}",
            "access_token": token,
        }
        res = requests.get(url, params=params, timeout=15).json()
        if "error" in res:
            return False, res["error"].get("message", "Invalid Meta token")

        pages = res.get("data", [])
        if not pages:
            # Token might be a Direct Page Access Token or user has no managed pages
            # Check /me
            me_res = requests.get(f"{GRAPH_API_BASE}/me", params={"fields": "id,name", "access_token": token}, timeout=15).json()
            if "id" in me_res:
                save_token_data({
                    "access_token": token,
                    "page_id": me_res["id"],
                    "page_name": me_res.get("name", "Facebook Account"),
                    "page_access_token": token,
                })
                return True, "Connected Facebook Page / Account"
            return False, "No Facebook Pages found for this account. Create a Facebook Page first to publish Reels."

        # Pick first page with connected Instagram or first page
        selected_page = None
        for p in pages:
            if p.get("instagram_business_account"):
                selected_page = p
                break
        if not selected_page:
            selected_page = pages[0]

        page_id = selected_page["id"]
        page_name = selected_page.get("name", "Facebook Page")
        page_token = selected_page.get("access_token", token)

        token_state: dict[str, Any] = {
            "access_token": token,
            "page_id": page_id,
            "page_name": page_name,
            "page_access_token": page_token,
        }

        ig_data = selected_page.get("instagram_business_account")
        if ig_data:
            token_state.update({
                "instagram_account_id": ig_data.get("id"),
                "instagram_username": ig_data.get("username", "Instagram Creator"),
                "instagram_profile_pic": ig_data.get("profile_picture_url", ""),
            })

        save_token_data(token_state)
        return True, "Connected successfully"
    except Exception as exc:
        return False, str(exc)


# ══════════════════════════════════════════════ viral reels metadata generator ══


REELS_EMOJIS = ["🔥", "💀", "😂", "🤯", "🚨", "🎯", "⚡️", "👀", "💥", "🤣", "😱", "🏆"]


def generate_reels_metadata(
    clip_title: str,
    clip_text: str,
    source_title: str,
    clip_index: int = 1,
) -> dict[str, Any]:
    """Generate high-engagement viral captions, hooks, and hashtags for Instagram & Facebook Reels."""
    combined = (clip_text + " " + source_title + " " + clip_title).lower()

    # 1. Topic detection
    topic = "gaming"
    for cand in ("gta 6", "gta vi", "gta 5", "gta v", "gta", "minecraft", "cod", "warzone", "roblox",
                 "fortnite", "valorant", "streamer", "react", "funny", "pubg", "apex", "csgo", "fifa"):
        if cand in combined:
            topic = cand
            break

    # 2. Hook and Emoji
    emoji = REELS_EMOJIS[(clip_index - 1) % len(REELS_EMOJIS)]
    sentences = [s.strip() for s in re.split(r"[.!?]+", clip_text) if s.strip()]
    punchline = sentences[0] if sentences else clip_title
    if len(punchline) > 55:
        punchline = punchline[:52] + "..."

    clean_title = clip_title.strip().rstrip(".!?,")
    if not clean_title:
        clean_title = punchline

    # 3. Call-To-Action (Rotated for variety)
    ctas = [
        "What would you do here? Drop your reaction in the comments! 👇",
        "Wait till the very end... 💀 Double tap if this was insane! ❤️",
        "Tag a friend who definitely needs to see this! 😂👇",
        "Rate this clip from 1 to 10 in the comments! 🔥",
        "Follow for daily viral moments & highlights! 🚀",
    ]
    cta = ctas[(clip_index - 1) % len(ctas)]

    # 4. Hashtags for Reels algorithm
    hashtags = ["#reels", "#reelsinstagram", "#viralreels", "#fyp", "#explorepage", "#trending"]
    if "gta" in topic:
        hashtags.extend(["#gta6", "#gtav", "#gtaonline", "#gtamodes", "#gamingreels", "#gamer", "#gamersofinstagram"])
    elif topic != "gaming":
        clean_top = topic.replace(" ", "")
        hashtags.extend([f"#{clean_top}", f"#{clean_top}reels", "#gamingreels", "#viralclips", "#gamingmemes"])
    else:
        hashtags.extend(["#gamingreels", "#gamers", "#gamingcommunity", "#streamer", "#twitchclips", "#funnyclips"])

    hashtags.extend(["#foryou", "#shortformcontent", "#instadaily"])

    # 5. Formatted Caption
    caption_lines = [
        f"{clean_title} {emoji}",
        "",
        f"👉 \"{punchline}\"",
        "",
        f"{cta}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"🎮 Featured: {source_title or 'Gaming Highlight'}",
        f"✂️ Moment #{clip_index}",
        "━━━━━━━━━━━━━━━━━━━━━",
        "",
        " ".join(hashtags[:20]),
    ]
    caption = "\n".join(caption_lines)

    return {
        "title": clean_title,
        "caption": caption,
        "hashtags": hashtags[:20],
        "emoji": emoji,
    }


# ═════════════════════════════════════════════════ upload to instagram reels ══


def upload_instagram_reel(
    video_path: Path,
    caption: str,
    share_to_feed: bool = True,
) -> dict[str, Any]:
    """Upload a 9:16 vertical video to Instagram Reels using the Resumable Upload protocol.
    
    Workflow:
    1. POST /{ig_user_id}/media with upload_type=resumable to obtain container id & rupload uri.
    2. POST raw video bytes directly to the rupload URI.
    3. Poll container until status_code == FINISHED.
    4. POST /{ig_user_id}/media_publish to publish the Reel.
    5. Retrieve published permalink.
    """
    data = load_token_data()
    ig_user_id = data.get("instagram_account_id")
    access_token = data.get("page_access_token") or data.get("access_token")

    if not ig_user_id or not access_token:
        raise RuntimeError("Instagram Business Account is not connected. Connect Meta in ClipForge settings first.")

    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    file_size = video_path.stat().st_size
    log.info("Starting Instagram Reel upload for %s (%d bytes)", video_path.name, file_size)

    # Step 1: Create Resumable Upload container
    init_url = f"{GRAPH_API_BASE}/{ig_user_id}/media"
    init_params = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": caption[:2200],
        "share_to_feed": "true" if share_to_feed else "false",
        "access_token": access_token,
    }
    init_res = requests.post(init_url, data=init_params, timeout=30)
    init_json = init_res.json()
    if init_res.status_code != 200 or "id" not in init_json:
        err = init_json.get("error", {}).get("message", init_res.text)
        raise RuntimeError(f"Instagram container creation failed: {err}")

    container_id = init_json["id"]
    rupload_uri = init_json.get("uri")
    if not rupload_uri:
        # Fallback rupload URI format if not in response
        rupload_uri = f"https://rupload.facebook.com/ig-reels-upload/{container_id}"

    # Step 2: Upload video binary data
    headers = {
        "Authorization": f"OAuth {access_token}",
        "offset": "0",
        "file_size": str(file_size),
        "Content-Type": "application/octet-stream",
    }
    with open(video_path, "rb") as vf:
        up_res = requests.post(rupload_uri, headers=headers, data=vf, timeout=120)
    if up_res.status_code not in (200, 201):
        err = up_res.text
        try:
            err = up_res.json().get("error", {}).get("message", err)
        except Exception:
            pass
        raise RuntimeError(f"Instagram binary upload failed: {err}")

    # Step 3: Poll container until processed
    status_url = f"{GRAPH_API_BASE}/{container_id}"
    ready = False
    for attempt in range(45):  # Wait up to ~90 seconds
        time.sleep(2)
        chk = requests.get(status_url, params={"fields": "status_code,status", "access_token": access_token}, timeout=15).json()
        sc = chk.get("status_code", "").upper()
        if sc == "FINISHED":
            ready = True
            break
        elif sc in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram video processing failed: {chk.get('status', 'Unknown error')}")

    if not ready:
        raise TimeoutError("Instagram video processing timed out after upload.")

    # Step 4: Publish container
    pub_url = f"{GRAPH_API_BASE}/{ig_user_id}/media_publish"
    pub_res = requests.post(pub_url, data={"creation_id": container_id, "access_token": access_token}, timeout=30).json()
    if "error" in pub_res:
        raise RuntimeError(f"Instagram Reel publish failed: {pub_res['error'].get('message')}")

    media_id = pub_res.get("id")
    permalink = f"https://www.instagram.com/reel/{media_id}"

    # Step 5: Fetch exact permalink if available
    try:
        media_info = requests.get(
            f"{GRAPH_API_BASE}/{media_id}",
            params={"fields": "permalink", "access_token": access_token},
            timeout=10,
        ).json()
        if media_info.get("permalink"):
            permalink = media_info["permalink"]
    except Exception:
        pass

    log.info("Instagram Reel published successfully: %s", permalink)
    return {
        "id": media_id,
        "url": permalink,
        "type": "instagram_reel",
        "container_id": container_id,
    }


# ══════════════════════════════════════════════════ upload to facebook reels ══


def upload_facebook_reel(
    video_path: Path,
    title: str,
    description: str,
    scheduled_publish_time: datetime.datetime | None = None,
) -> dict[str, Any]:
    """Upload a 9:16 vertical video to Facebook Page Reels using the Video Reels API.
    
    Workflow:
    1. POST /{page_id}/video_reels with upload_phase=start to initialize session.
    2. POST raw video bytes to the provided upload_url.
    3. POST /{page_id}/video_reels with upload_phase=finish to publish or schedule.
    """
    data = load_token_data()
    page_id = data.get("page_id")
    access_token = data.get("page_access_token") or data.get("access_token")

    if not page_id or not access_token:
        raise RuntimeError("Facebook Page is not connected. Connect Meta in ClipForge settings first.")

    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    file_size = video_path.stat().st_size
    log.info("Starting Facebook Reel upload for %s (%d bytes)", video_path.name, file_size)

    # Step 1: Start upload session
    init_url = f"{GRAPH_API_BASE}/{page_id}/video_reels"
    init_res = requests.post(
        init_url,
        data={"upload_phase": "start", "access_token": access_token},
        timeout=30,
    ).json()

    if "error" in init_res:
        raise RuntimeError(f"Facebook Reels initialization failed: {init_res['error'].get('message')}")

    video_id = init_res.get("video_id")
    upload_url = init_res.get("upload_url")
    if not video_id or not upload_url:
        raise RuntimeError("Facebook Reels did not return video_id or upload_url.")

    # Step 2: Upload raw video data
    headers = {
        "Authorization": f"OAuth {access_token}",
        "offset": "0",
        "file_size": str(file_size),
        "Content-Type": "application/octet-stream",
    }
    with open(video_path, "rb") as vf:
        up_res = requests.post(upload_url, headers=headers, data=vf, timeout=120)
    if up_res.status_code not in (200, 201):
        err = up_res.text
        try:
            err = up_res.json().get("error", {}).get("message", err)
        except Exception:
            pass
        raise RuntimeError(f"Facebook binary upload failed: {err}")

    # Step 3: Finish and Publish / Schedule
    finish_data: dict[str, Any] = {
        "upload_phase": "finish",
        "access_token": access_token,
        "video_id": video_id,
        "title": title[:100],
        "description": description[:5000],
    }

    if scheduled_publish_time is not None:
        finish_data["video_state"] = "SCHEDULED"
        finish_data["scheduled_publish_time"] = int(scheduled_publish_time.timestamp())
    else:
        finish_data["video_state"] = "PUBLISHED"

    fin_res = requests.post(init_url, data=finish_data, timeout=30).json()
    if "error" in fin_res:
        raise RuntimeError(f"Facebook Reels publish failed: {fin_res['error'].get('message')}")

    fb_url = f"https://www.facebook.com/reel/{video_id}"
    log.info("Facebook Reel published successfully: %s", fb_url)

    return {
        "id": video_id,
        "url": fb_url,
        "type": "facebook_reel",
        "scheduled": scheduled_publish_time.isoformat() if scheduled_publish_time else None,
    }


def get_latest_scheduled_time(jobs_dict: dict | None = None) -> datetime.datetime:
    """Find the latest scheduled Meta Reels publishing time across all existing jobs."""
    now = datetime.datetime.now(datetime.timezone.utc)
    latest = now
    if jobs_dict:
        for j in jobs_dict.values():
            for c in j.get("clips", []):
                sched_str = c.get("meta_scheduled")
                if sched_str and c.get("meta_status") == "scheduled":
                    try:
                        clean_str = sched_str.replace("Z", "+00:00")
                        dt = datetime.datetime.fromisoformat(clean_str)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=datetime.timezone.utc)
                        if dt > latest:
                            latest = dt
                    except Exception:
                        pass
    return latest
