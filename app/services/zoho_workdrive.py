"""Zoho WorkDrive API service — file upload, download, and folder management.

Uses the same OAuth2 refresh-token flow as ZohoBookingsService but targets
the WorkDrive API endpoints. Requires WorkDrive scopes to be included in the
refresh token (WorkDrive.files.ALL).
"""
from __future__ import annotations

import time
from io import BytesIO
from typing import Optional

import requests

from app.core.config import settings

_BASE = "https://workdrive.zoho.{dc}/api/v1"
_AUTH_URL = "https://accounts.zoho.{dc}/oauth/v2/token"

_token_cache: dict = {"token": None, "expires_at": 0}


def _get_access_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    dc = settings.ZOHO_DC or "com"
    resp = requests.post(
        _AUTH_URL.replace("{dc}", dc),
        data={
            "grant_type": "refresh_token",
            "client_id": settings.ZOHO_CLIENT_ID,
            "client_secret": settings.ZOHO_CLIENT_SECRET,
            "refresh_token": settings.ZOHO_REFRESH_TOKEN,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Zoho WorkDrive token refresh failed: {data}")

    _token_cache["token"] = token
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600)) - 60
    return token


def _headers() -> dict:
    return {
        "Authorization": f"Zoho-oauthtoken {_get_access_token()}",
        "Accept": "application/json",
    }


def _base_url() -> str:
    return _BASE.replace("{dc}", settings.ZOHO_DC or "com")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upload_file(
    file_content: bytes,
    filename: str,
    folder_id: str | None = None,
    mime_type: str = "application/octet-stream",
) -> str:
    """Upload a file to Zoho WorkDrive. Returns the file ID."""
    folder = folder_id or settings.ZOHO_WORKDRIVE_FOLDER_ID
    url = f"{_base_url()}/upload"
    files = {"content": (filename, BytesIO(file_content), mime_type)}
    data = {
        "filename": filename,
        "parent_id": folder,
        "override-name-exist": "true",
    }
    resp = requests.post(url, headers=_headers(), files=files, data=data, timeout=60)
    resp.raise_for_status()
    result = resp.json()
    # WorkDrive returns {"data": {"attributes": {"resource_id": "..."}}}
    try:
        return result["data"]["attributes"]["resource_id"]
    except (KeyError, TypeError):
        raise RuntimeError(f"WorkDrive upload unexpected response: {result}")


def download_file(file_id: str) -> bytes:
    """Download file content by file ID."""
    url = f"{_base_url()}/files/{file_id}/download"
    resp = requests.get(url, headers=_headers(), timeout=60)
    resp.raise_for_status()
    return resp.content


def get_file_info(file_id: str) -> dict:
    """Get file metadata."""
    url = f"{_base_url()}/files/{file_id}"
    resp = requests.get(url, headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_download_url(file_id: str) -> Optional[str]:
    """Return a viewable URL for the file (may require auth)."""
    if not file_id:
        return None
    dc = settings.ZOHO_DC or "com"
    return f"https://workdrive.zoho.{dc}/file/{file_id}"
