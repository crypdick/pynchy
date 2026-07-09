"""OAuth token exchange and authorization flow for Google Setup."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from pynchy.logger import logger
from pynchy.plugins.integrations.google_setup._paths import (
    GOOGLE_AUTH_URL,
    GOOGLE_TOKEN_URL,
    OAUTH_CALLBACK_PORT,
    credentials_path,
)


def parse_client_credentials(kp: Path) -> tuple[str, str]:
    """Extract client_id and client_secret from the GCP OAuth JSON."""
    with kp.open(encoding="utf-8") as f:
        data = json.load(f)
    client = data.get("installed") or data.get("web")
    if not client:
        raise RuntimeError("Invalid credentials JSON")
    return client["client_id"], client["client_secret"]


def build_auth_url(client_id: str, scopes: str) -> str:
    """Build the Google OAuth authorization URL."""
    params = {
        "client_id": client_id,
        "redirect_uri": f"http://localhost:{OAUTH_CALLBACK_PORT}",
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
        def do_GET(self):
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

        def log_message(self, *args):
            pass

    server = HTTPServer(("0.0.0.0", OAUTH_CALLBACK_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return done, auth_codes, server


def exchange_code_for_tokens(code: str, client_id: str, client_secret: str) -> dict[str, Any]:
    """Exchange the authorization code for access + refresh tokens."""
    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": f"http://localhost:{OAUTH_CALLBACK_PORT}",
            "grant_type": "authorization_code",
        }
    ).encode()

    req = urllib.request.Request(
        GOOGLE_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as resp:
        tokens: dict[str, Any] = json.loads(resp.read())

    if "error" in tokens:
        raise RuntimeError(f"Token exchange failed: {tokens['error']}")

    # Add expiry_date (ms) as expected by the googleapis Node.js client
    if "expires_in" in tokens:
        tokens["expiry_date"] = int(time.time() * 1000) + tokens["expires_in"] * 1000

    return tokens


def save_credentials_to_profile(tokens: dict[str, Any], profile_name: str) -> Path:
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


async def run_oauth_flow(page, kp: Path, scopes: str) -> dict[str, Any]:
    """Run the OAuth consent + token exchange flow."""
    client_id, client_secret = parse_client_credentials(kp)
    done_event, auth_codes, callback_server = start_callback_server()

    auth_url = build_auth_url(client_id, scopes)
    await page.goto(auth_url, wait_until="domcontentloaded")

    logger.info("Waiting for OAuth consent (click Allow in the browser)")

    deadline = time.time() + 300  # 5 minutes
    while not done_event.is_set() and time.time() < deadline:
        await asyncio.sleep(0.5)

    callback_server.shutdown()

    if not auth_codes:
        raise RuntimeError(
            "OAuth callback not received within 5 minutes. "
            "Make sure you clicked 'Allow' in the browser."
        )

    logger.info("Exchanging authorization code for tokens")
    tokens = exchange_code_for_tokens(auth_codes[0], client_id, client_secret)

    if "refresh_token" not in tokens:
        logger.warning("No refresh_token received — access token will expire")

    return tokens
