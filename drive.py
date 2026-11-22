"""ClipForge – Google Drive upload integration.

Handles OAuth2 authentication and resumable file uploads to Google Drive.
Google client libraries are imported lazily so the rest of ClipForge works
even when they are not installed.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

# ── data directory (matches server.py convention) ────────────────────────────
ROOT = Path(__file__).parent
DATA = Path(os.environ.get("CLIPFORGE_DATA", ROOT / "data")).resolve()

# Allow HTTP transport for local/reverse-proxy OAuth flow
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_PATH = DATA / "drive_token.json"

log = logging.getLogger("clipforge.drive")


# ═══════════════════════════════════════════════════════════════════ helpers ══


def _client_config() -> dict:
    """Return an OAuth2 web client config built from env vars."""
    cid = (os.environ.get("GOOGLE_CLIENT_ID") or os.environ.get("CLIENT_ID") or "").strip()
    sec = (os.environ.get("GOOGLE_CLIENT_SECRET") or os.environ.get("CLIENT_SECRET") or "").strip()
    return {
        "web": {
            "client_id": cid,
            "client_secret": sec,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def _load_credentials():
    """Load and auto-refresh stored OAuth2 credentials. Returns None on failure."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN_PATH.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    except Exception:
        log.warning("drive_token.json is corrupt – treating as disconnected")
        return None

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        except Exception as exc:
            log.warning("Token refresh failed: %s", exc)
            return None

    return creds if creds.valid else None


def _build_service():
    """Return an authenticated Drive v3 service, or raise."""
    from googleapiclient.discovery import build

    creds = _load_credentials()
    if creds is None:
        raise RuntimeError("Google Drive is not connected. Please authenticate first.")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ═══════════════════════════════════════════════════════════════ auth status ══


def is_configured() -> bool:
    """True when the required env vars are set (regardless of connection)."""
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def is_connected() -> bool:
    """True when a usable saved token exists."""
    if not TOKEN_PATH.exists():
        return False
    try:
        creds = _load_credentials()
        return creds is not None
    except Exception:
        return False


def get_auth_url(redirect_uri: str) -> str:
    """Return a Google OAuth2 authorisation URL the user should visit."""
    import urllib.parse

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    if not is_configured():
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
    """Exchange an authorisation code for tokens and persist them."""
    import json
    import urllib.parse
    import urllib.request
    from google.oauth2.credentials import Credentials

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    if not is_configured():
        return False, "GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET environment variables are missing."

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
        print(f"[clipforge drive callback error] {msg}")
        return False, msg


def disconnect() -> bool:
    """Delete the saved token, effectively logging out of Drive.

    Returns ``True`` if the token was removed, ``False`` if none existed.
    """
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()
        return True
    return False


def get_status() -> dict[str, Any]:
    """Return a dict describing the current Drive integration state.

    Keys: ``configured`` (bool), ``connected`` (bool), ``email`` (str | None).
    """
    status: dict[str, Any] = {
        "configured": is_configured(),
        "connected": False,
        "email": None,
    }
    if not status["configured"]:
        return status

    creds = None
    try:
        creds = _load_credentials()
    except Exception:
        pass

    if creds is None:
        return status

    status["connected"] = True

    # Try to extract the email from the id_token embedded in the credential
    # (available after a fresh OAuth consent flow).
    try:
        token_data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
        # google-auth stores token info alongside the token itself
        if "client_id" in token_data:
            # Fetch userinfo to get the email
            from googleapiclient.discovery import build

            svc = build("oauth2", "v2", credentials=creds, cache_discovery=False)
            info = svc.userinfo().get().execute()
            status["email"] = info.get("email")
    except Exception:
        pass  # email is best-effort

    return status


# ═══════════════════════════════════════════════════════════════════ upload ══


def _find_or_create_folder(service, name: str, parent_id: str | None = None) -> str:
    """Return the Drive folder ID for *name* under *parent_id*, creating it if
    it does not exist.
    """
    query = (
        f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    if parent_id:
        query += f" and '{parent_id}' in parents"

    results = (
        service.files()
        .list(q=query, spaces="drive", fields="files(id, name)", pageSize=1)
        .execute()
    )
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    # Create the folder
    metadata: dict[str, Any] = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]

    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def upload_clips(
    job_id: str,
    job_title: str,
    clip_files: list[Path],
) -> dict[str, Any]:
    """Upload *clip_files* to Google Drive.

    Folder structure::

        My Drive/
          ClipForge/
            {job_title}_{job_id[:8]}/
              clip1.mp4
              clip2.mp4

    Returns ``{"folder_id": "<id>", "uploaded": <count>}``.
    Raises on auth or API errors.
    """
    from googleapiclient.http import MediaFileUpload

    service = _build_service()

    # Ensure folder hierarchy
    root_folder_id = _find_or_create_folder(service, "ClipForge")
    safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in job_title).strip()
    sub_name = f"{safe_title}_{job_id[:8]}"
    sub_folder_id = _find_or_create_folder(service, sub_name, parent_id=root_folder_id)

    uploaded = 0
    for clip in clip_files:
        if not clip.exists():
            log.warning("Clip file not found, skipping: %s", clip)
            continue

        mime = "video/mp4"
        if clip.suffix.lower() in (".webm",):
            mime = "video/webm"
        elif clip.suffix.lower() in (".mkv",):
            mime = "video/x-matroska"

        media = MediaFileUpload(
            str(clip),
            mimetype=mime,
            resumable=True,
            chunksize=10 * 1024 * 1024,  # 10 MiB chunks
        )

        file_metadata: dict[str, Any] = {
            "name": clip.name,
            "parents": [sub_folder_id],
        }

        request = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id",
        )

        # Resumable upload loop
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                log.info("Upload %s: %d%%", clip.name, int(status.progress() * 100))

        uploaded += 1
        log.info("Uploaded %s (id=%s)", clip.name, response.get("id"))

    return {"folder_id": sub_folder_id, "uploaded": uploaded}
