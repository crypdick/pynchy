"""OAuth token exchange and authorization flow for Google Setup."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path  # noqa: TC003 - beartype resolves this runtime annotation.
from typing import Protocol, runtime_checkable

from pynchy.logger import logger
from pynchy.plugins.integrations.google_setup._paths import (
    GOOGLE_AUTH_URL,
    GOOGLE_OAUTH_ENDPOINT_URL,
    OAUTH_CALLBACK_PORT,
    credentials_path,
)

OAUTH_CALLBACK_HOST = "localhost"
INVALID_CREDENTIALS_JSON_ERROR = "Invalid credentials JSON"
GOOGLE_API_URL_MUST_USE_HTTPS_ERROR = "Google API URL must use https"
OAUTH_CALLBACK_TIMEOUT_ERROR = (
    "OAuth callback not received within 5 minutes. Make sure you clicked 'Allow' in the browser."
)


def _token_exchange_failure_message(error: object) -> str:
    return f"Token exchange failed: {error}"


@runtime_checkable
class OAuthPage(Protocol):
    """The browser surface required to complete the OAuth consent flow."""

    async def goto(self, url: str, *, wait_until: str) -> object: ...  # noqa: V107


def parse_client_credentials(kp: Path) -> tuple[str, str]:
    """Extract client_id and client_secret from the GCP OAuth JSON."""
    with kp.open(encoding="utf-8") as f:
        data = json.load(f)
    client = data.get("installed") or data.get("web")
    if not client:
        raise RuntimeError(INVALID_CREDENTIALS_JSON_ERROR)
    return client["client_id"], client["client_secret"]


def build_auth_url(client_id: str, scopes: str) -> str:
    """Build the Google OAuth authorization URL."""
    params = {
        "client_id": client_id,
        "redirect_uri": f"http://{OAUTH_CALLBACK_HOST}:{OAUTH_CALLBACK_PORT}",
        "response_type": "code",
        "scope": scopes,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def start_callback_server() -> tuple[threading.Event, list[str], HTTPServer]:
    """Start HTTP server to receive the OAuth callback."""
    auth_codes: list[str] = []
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: V105
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = query.get("code", [None])[0]
            if code:
                auth_codes.append(code)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Authorization successful!</h2>"
                b"<p>You can close this tab. Setup will continue.</p>"
                b"</body></html>"
            )
            done.set()

        def log_message(self, *args: object) -> None:  # noqa: V105
            pass

    server = HTTPServer((OAUTH_CALLBACK_HOST, OAUTH_CALLBACK_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return done, auth_codes, server


def exchange_code_for_tokens(code: str, client_id: str, client_secret: str) -> dict[str, object]:
    """Exchange the authorization code for access + refresh tokens."""
    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": f"http://{OAUTH_CALLBACK_HOST}:{OAUTH_CALLBACK_PORT}",
            "grant_type": "authorization_code",
        }
    ).encode()

    req = urllib.request.Request(  # noqa: S310 - opened only through the HTTPS-gated helper.
        GOOGLE_OAUTH_ENDPOINT_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen_https_request(req) as resp:
        tokens: dict[str, object] = json.loads(resp.read())

    if "error" in tokens:
        raise RuntimeError(_token_exchange_failure_message(tokens["error"]))

    # Add expiry_date (ms) as expected by the googleapis Node.js client
    expires_in = tokens.get("expires_in")
    if isinstance(expires_in, int):
        tokens["expiry_date"] = int(time.time() * 1000) + expires_in * 1000

    return tokens


def urlopen_https_request(req: urllib.request.Request) -> object:
    scheme = urllib.parse.urlsplit(req.full_url).scheme.lower()
    if scheme != "https":
        raise RuntimeError(GOOGLE_API_URL_MUST_USE_HTTPS_ERROR)
    return urllib.request.urlopen(req)  # noqa: S310 - scheme is constrained above.


def save_credentials_to_profile(tokens: dict[str, object], profile_name: str) -> Path:
    """Write credentials.json to the chrome profile directory."""
    dest = credentials_path(profile_name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(tokens, indent=2))
    logger.info(
        "OAuth tokens saved to chrome profile",
        profile=profile_name,
        path=str(dest),
        has_refresh_token="refresh_token" in tokens,
    )
    return dest


async def run_oauth_flow(page: OAuthPage, kp: Path, scopes: str) -> dict[str, object]:
    """Run the OAuth consent + token exchange flow."""
    client_id, client_secret = parse_client_credentials(kp)
    done_event, auth_codes, callback_server = start_callback_server()

    auth_url = build_auth_url(client_id, scopes)
    await page.goto(auth_url, wait_until="domcontentloaded")

    logger.info("Waiting for OAuth consent (click Allow in the browser)")

    callback_received = await asyncio.to_thread(done_event.wait, 300)

    callback_server.shutdown()

    if not callback_received or not auth_codes:
        raise RuntimeError(OAUTH_CALLBACK_TIMEOUT_ERROR)

    logger.info("Exchanging authorization code for tokens")
    tokens = exchange_code_for_tokens(auth_codes[0], client_id, client_secret)

    if "refresh_token" not in tokens:
        logger.warning("No refresh_token received — access token will expire")

    return tokens
