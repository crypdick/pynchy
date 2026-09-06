"""Service Usage REST API helpers for Google Setup.

Reading project metadata from stored credentials, refreshing access tokens,
and enabling Google APIs without needing browser automation when possible.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from pynchy.logger import logger
from pynchy.plugins.integrations.google_setup._oauth import (
    parse_client_credentials,
    urlopen_https_request,
)
from pynchy.plugins.integrations.google_setup._paths import (
    GOOGLE_OAUTH_ENDPOINT_URL,
    SERVICE_USAGE_URL,
    credentials_path,
    keys_path,
)


def get_project_number(kp: Path) -> str | None:
    """Extract the GCP project number from the OAuth client_id."""
    try:
        with kp.open(encoding="utf-8") as f:
            data = json.load(f)
        client = data.get("installed") or data.get("web")
        if client and client.get("client_id"):
            return str(client["client_id"]).split("-", 1)[0]
    except (OSError, ValueError, KeyError) as exc:
        logger.debug("Could not extract project number from credentials", error=str(exc))
    return None


def read_project_id(kp: Path) -> str | None:
    """Auto-detect project ID from existing credentials JSON."""
    if not kp.exists():
        return None
    try:
        with kp.open(encoding="utf-8") as f:
            data = json.load(f)
        client = data.get("installed") or data.get("web")
        if client and client.get("project_id"):
            return str(client["project_id"])
    except (OSError, ValueError, KeyError) as exc:
        logger.debug("Could not read project id from credentials", error=str(exc))
    return None


def refresh_access_token(profile_name: str) -> str | None:
    """Refresh the OAuth access token using stored credentials.

    Reads from chrome profile directory (not Docker volume).
    """
    kp = keys_path(profile_name)
    try:
        client_id, client_secret = parse_client_credentials(kp)
    except (OSError, ValueError, KeyError) as exc:
        logger.debug("Could not parse client credentials for refresh", error=str(exc))
        return None

    refresh_token = _stored_refresh_token(profile_name)
    if refresh_token is None:
        return None

    data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request(  # noqa: S310 - opened only through the HTTPS-gated helper.
        GOOGLE_OAUTH_ENDPOINT_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen_https_request(req) as resp:
            tokens = json.loads(resp.read())
        access_token = tokens.get("access_token")
        return access_token if isinstance(access_token, str) else None
    except (RuntimeError, urllib.error.URLError, ValueError) as exc:
        logger.debug("Access token refresh failed", error=str(exc))
        return None


def _stored_refresh_token(profile_name: str) -> str | None:
    creds_path = credentials_path(profile_name)
    try:
        creds = json.loads(creds_path.read_text())
        refresh_token = creds.get("refresh_token")
    except (OSError, ValueError) as exc:
        logger.debug("Could not read stored refresh token", error=str(exc))
        return None
    return refresh_token if isinstance(refresh_token, str) and refresh_token else None


def enable_api_via_rest(project_number: str, access_token: str, api_id: str) -> bool:
    """Enable a Google API via the Service Usage REST API."""
    url = f"{SERVICE_USAGE_URL}/projects/{project_number}/services/{api_id}:enable"
    req = urllib.request.Request(  # noqa: S310 - opened only through the HTTPS-gated helper.
        url,
        data=b"{}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen_https_request(req) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        if "SCOPE_INSUFFICIENT" in body or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in body:
            logger.info("REST enable failed (insufficient scopes)", api=api_id)
        else:
            logger.warning("REST enable failed", api=api_id, status=exc.code, body=body[:200])
        return False
    except (RuntimeError, urllib.error.URLError) as exc:
        logger.warning("REST enable failed", api=api_id, error=str(exc))
        return False
    else:
        logger.info("API enabled via REST", api=api_id, result_name=result.get("name", ""))
        return True
