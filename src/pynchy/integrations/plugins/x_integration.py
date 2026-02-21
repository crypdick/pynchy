"""Built-in X (Twitter) integration plugin (service handler).

Provides host-side handlers for X/Twitter actions (post, like, reply, retweet,
quote) via Playwright browser automation with a persistent Chromium profile.
Uses the system Chrome binary (``CHROME_PATH``) in headed mode to avoid
X's bot detection — Playwright's bundled Chromium is never used.

Six handlers:
- ``setup_x_session`` — headed browser for manual X login (noVNC on headless servers)
- ``x_post`` — post a tweet (max 280 chars)
- ``x_like`` — like a tweet
- ``x_reply`` — reply to a tweet
- ``x_retweet`` — retweet
- ``x_quote`` — quote tweet with comment

The container-side IPC relay (_tools_x.py) sends service requests through IPC;
the host service handler dispatches to these handlers after policy enforcement.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pluggy

from pynchy.integrations.browser import (
    chrome_path,
    cleanup_lock_files,
    has_display,
    profile_dir,
    stop_procs,
)
from pynchy.logger import logger

if TYPE_CHECKING:
    from playwright.async_api import Page

hookimpl = pluggy.HookimplMarker("pynchy")


def _check_system_deps() -> None:
    """Validate CHROME_PATH and warn about missing headless-server packages."""
    try:
        chrome_path()
    except RuntimeError as e:
        logger.warning("X integration system dep check failed", error=str(e))
        return

    if not os.environ.get("DISPLAY"):
        missing = [t for t in ("Xvfb", "x11vnc", "websockify") if not shutil.which(t)]
        if missing:
            logger.warning(
                "Headless server — setup_x_session needs VNC deps",
                missing=missing,
            )


# ---------------------------------------------------------------------------
# Persistent Xvfb display (X tools always use headed mode)
# ---------------------------------------------------------------------------

_XVFB_DISPLAY = ":99"
_VNC_PORT = 5999
_NOVNC_PORT = 6080
_NOVNC_WEB_DIR = "/usr/share/novnc"

# Module-level Xvfb process.  X tools use headed mode to avoid bot detection,
# so Xvfb persists for the lifetime of this plugin on headless hosts.
_xvfb_proc: subprocess.Popen | None = None


def _ensure_xvfb() -> None:
    """Ensure Xvfb is running. X needs headed mode to avoid bot detection.

    Starts Xvfb once and keeps it running for the lifetime of the server.
    Safe to call multiple times — subsequent calls are no-ops if Xvfb is
    already running or a native display is available.
    """
    global _xvfb_proc  # noqa: PLW0603
    if has_display():
        return
    if _xvfb_proc is not None and _xvfb_proc.poll() is None:
        os.environ["DISPLAY"] = _XVFB_DISPLAY
        return
    if not shutil.which("Xvfb"):
        raise RuntimeError(
            "No display available and Xvfb not installed. X automation requires "
            "headed mode to avoid bot detection. Install with: apt install xvfb"
        )
    _xvfb_proc = subprocess.Popen(
        ["Xvfb", _XVFB_DISPLAY, "-screen", "0", "1280x720x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)
    if _xvfb_proc.poll() is not None:
        code = _xvfb_proc.returncode
        _xvfb_proc = None
        raise RuntimeError(f"Xvfb exited immediately (code {code})")
    os.environ["DISPLAY"] = _XVFB_DISPLAY


def _start_vnc_layer() -> tuple[list[subprocess.Popen], str]:
    """Start x11vnc + noVNC on the existing Xvfb display.

    Returns (processes, novnc_url).  Call ``_ensure_xvfb()`` first.
    """
    missing = [t for t in ("x11vnc", "websockify") if not shutil.which(t)]
    if missing:
        raise RuntimeError(
            f"VNC layer requires: {', '.join(missing)}. Install with: apt install x11vnc novnc"
        )
    procs: list[subprocess.Popen] = []
    try:
        x11vnc = subprocess.Popen(
            [
                "x11vnc",
                "-display",
                _XVFB_DISPLAY,
                "-forever",
                "-nopw",
                "-rfbport",
                str(_VNC_PORT),
                "-quiet",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(x11vnc)
        time.sleep(0.5)
        if x11vnc.poll() is not None:
            raise RuntimeError(f"x11vnc exited immediately (code {x11vnc.returncode})")

        ws_cmd = ["websockify", str(_NOVNC_PORT), f"localhost:{_VNC_PORT}"]
        if os.path.isdir(_NOVNC_WEB_DIR):
            ws_cmd[1:1] = ["--web", _NOVNC_WEB_DIR]
        websockify_proc = subprocess.Popen(
            ws_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(websockify_proc)
        time.sleep(0.5)
        if websockify_proc.poll() is not None:
            raise RuntimeError(f"websockify exited immediately (code {websockify_proc.returncode})")

        return procs, f"http://HOST:{_NOVNC_PORT}/vnc.html?autoconnect=true"

    except Exception:
        stop_procs(procs)
        raise


def _cleanup_xvfb() -> None:
    global _xvfb_proc  # noqa: PLW0603
    if _xvfb_proc and _xvfb_proc.poll() is None:
        _xvfb_proc.terminate()
        try:
            _xvfb_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _xvfb_proc.kill()
    _xvfb_proc = None


atexit.register(_cleanup_xvfb)


# ---------------------------------------------------------------------------
# Playwright helpers
# ---------------------------------------------------------------------------

# X UI selectors (data-testid based).  These match X's React component
# test IDs and are the same ones the archived TS implementation used.
_SEL = {
    "tweet_input": '[data-testid="tweetTextarea_0"]',
    "post_button": '[data-testid="tweetButtonInline"]',
    "reply_button": '[data-testid="reply"]',
    "like": '[data-testid="like"]',
    "unlike": '[data-testid="unlike"]',
    "retweet": '[data-testid="retweet"]',
    "unretweet": '[data-testid="unretweet"]',
    "retweet_confirm": '[data-testid="retweetConfirm"]',
    "modal": '[role="dialog"][aria-modal="true"]',
    "modal_submit": '[data-testid="tweetButton"]',
    "account_switcher": '[data-testid="SideNav_AccountSwitcher_Button"]',
    "login_input": 'input[autocomplete="username"]',
    "tweet_article": 'article[data-testid="tweet"]',
}

_TIMEOUTS = {
    "navigation": 30_000,
    "element": 5_000,
    "after_click": 1_000,
    "after_fill": 1_000,
    "after_submit": 3_000,
    "page_load": 3_000,
}

_TWEET_MAX = 280

# Anti-detection launch args.  These suppress Playwright's automation
# fingerprints that X actively checks for.
_BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-sync",
]


async def _is_visible(locator) -> bool:
    """Check locator visibility without raising on detached elements."""
    try:
        return await locator.is_visible()
    except Exception:
        return False


def _validate_content(content: str | None, label: str = "Tweet") -> str | None:
    """Validate tweet content.  Returns error string or None if valid."""
    if not content:
        return f"{label} content cannot be empty"
    if len(content) > _TWEET_MAX:
        return f"{label} exceeds {_TWEET_MAX} char limit (current: {len(content)})"
    return None


async def _navigate_to_tweet(page: Page, tweet_url: str) -> str | None:
    """Navigate to a tweet page.  Returns error message or None on success."""
    url = tweet_url.strip()
    if re.match(r"^\d+$", url):
        url = f"https://x.com/i/status/{url}"
    elif not url.startswith("http"):
        url = f"https://{url}"

    try:
        await page.goto(
            url,
            timeout=_TIMEOUTS["navigation"],
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(_TIMEOUTS["page_load"])
    except Exception as exc:
        return f"Navigation failed: {exc}"

    if not await _is_visible(page.locator(_SEL["tweet_article"]).first):
        return "Tweet not found. It may have been deleted or the URL is invalid."
    return None


def _launch_kwargs(profile_path: Path) -> dict:
    """Build kwargs for ``launch_persistent_context``.

    Always uses the system Chrome binary (``CHROME_PATH``) for a genuine
    browser fingerprint.  Playwright provides only the automation protocol.
    """
    return {
        "user_data_dir": str(profile_path),
        "executable_path": chrome_path(),
        "headless": False,
        "args": _BROWSER_ARGS,
        "ignore_default_args": ["--enable-automation"],
    }


async def _with_browser(
    fn: Callable[[Page], Awaitable[dict]],
) -> dict:
    """Run *fn(page)* inside a persistent browser context.

    Manages Xvfb display, lock-file cleanup, Playwright lifecycle.
    Used by action tools (``setup_x_session`` has its own VNC flow).
    """
    from playwright.async_api import async_playwright

    _ensure_xvfb()
    x_profile = profile_dir("x")
    cleanup_lock_files(x_profile)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            **_launch_kwargs(x_profile),
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            return await fn(page)
        finally:
            await context.close()


async def _check_login(page: Page) -> str | None:
    """Return an error string if not logged in, None if OK."""
    if await _is_visible(page.locator(_SEL["account_switcher"])):
        return None
    if await _is_visible(page.locator(_SEL["login_input"])):
        return "X login expired. Run setup_x_session to re-authenticate."
    # Page may still be loading — don't fail yet
    return None


# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------


async def _handle_setup_x_session(data: dict) -> dict:
    """Launch a headed browser for manual X login. Saves the session."""
    from playwright.async_api import async_playwright

    timeout_seconds = data.get("timeout_seconds", 120)
    vnc_procs: list[subprocess.Popen] = []
    novnc_url: str | None = None

    try:
        need_vnc = not has_display()
        _ensure_xvfb()
        if need_vnc:
            vnc_procs, novnc_url = _start_vnc_layer()

        x_profile = profile_dir("x")
        cleanup_lock_files(x_profile)

        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                **_launch_kwargs(x_profile),
            )
            page = context.pages[0] if context.pages else await context.new_page()

            await page.goto(
                "https://x.com/login",
                timeout=_TIMEOUTS["navigation"],
                wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(_TIMEOUTS["page_load"])

            # Already logged in?
            if await _is_visible(page.locator(_SEL["account_switcher"])):
                await context.close()
                result: dict[str, Any] = {
                    "status": "ok",
                    "message": f"Already logged in to X. Profile saved at {x_profile}",
                }
                if novnc_url:
                    result["novnc_url"] = novnc_url
                return {"result": result}

            # Wait for human to complete login
            try:
                await page.wait_for_selector(
                    _SEL["account_switcher"],
                    timeout=timeout_seconds * 1000,
                )
            except Exception:
                await context.close()
                return {
                    "error": (
                        f"Login not completed within {timeout_seconds}s. "
                        "Try again with a longer timeout."
                    )
                }

            await context.close()

        result = {
            "status": "ok",
            "profile_dir": str(x_profile),
            "message": "X session saved. Future tool calls will use this session.",
        }
        if novnc_url:
            result["novnc_url"] = novnc_url
        return {"result": result}

    except Exception as exc:
        logger.error("X session setup failed", error=str(exc))
        return {"error": str(exc)}

    finally:
        stop_procs(vnc_procs)


async def _handle_x_post(data: dict) -> dict:
    """Post a tweet on X (Twitter)."""
    content = data.get("content", "")
    error = _validate_content(content)
    if error:
        return {"error": error}

    async def action(page: Page) -> dict:
        await page.goto(
            "https://x.com/home",
            timeout=_TIMEOUTS["navigation"],
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(_TIMEOUTS["page_load"])

        login_err = await _check_login(page)
        if login_err:
            return {"error": login_err}

        tweet_input = page.locator(_SEL["tweet_input"])
        await tweet_input.wait_for(timeout=_TIMEOUTS["element"] * 2)
        await tweet_input.click()
        await page.wait_for_timeout(_TIMEOUTS["after_click"] // 2)
        await tweet_input.fill(content)
        await page.wait_for_timeout(_TIMEOUTS["after_fill"])

        post_btn = page.locator(_SEL["post_button"])
        await post_btn.wait_for(timeout=_TIMEOUTS["element"])
        if await post_btn.get_attribute("aria-disabled") == "true":
            return {"error": "Post button disabled. Content may be empty or exceed limit."}

        await post_btn.click()
        await page.wait_for_timeout(_TIMEOUTS["after_submit"])

        preview = content[:50] + ("..." if len(content) > 50 else "")
        return {"result": {"status": "ok", "message": f"Tweet posted: {preview}"}}

    try:
        return await _with_browser(action)
    except Exception as exc:
        logger.error("X post failed", error=str(exc))
        return {"error": str(exc)}


async def _handle_x_like(data: dict) -> dict:
    """Like a tweet on X (Twitter)."""
    tweet_url = data.get("tweet_url", "")
    if not tweet_url:
        return {"error": "Please provide a tweet URL"}

    async def action(page: Page) -> dict:
        nav_err = await _navigate_to_tweet(page, tweet_url)
        if nav_err:
            return {"error": nav_err}

        tweet = page.locator(_SEL["tweet_article"]).first

        if await _is_visible(tweet.locator(_SEL["unlike"])):
            return {"result": {"status": "ok", "message": "Tweet already liked"}}

        like_btn = tweet.locator(_SEL["like"])
        await like_btn.wait_for(timeout=_TIMEOUTS["element"])
        await like_btn.click()
        await page.wait_for_timeout(_TIMEOUTS["after_click"])

        if await _is_visible(tweet.locator(_SEL["unlike"])):
            return {"result": {"status": "ok", "message": "Like successful"}}

        return {
            "result": {
                "status": "ok",
                "message": "Like action completed but could not verify success",
            }
        }

    try:
        return await _with_browser(action)
    except Exception as exc:
        logger.error("X like failed", error=str(exc))
        return {"error": str(exc)}


async def _handle_x_reply(data: dict) -> dict:
    """Reply to a tweet on X (Twitter)."""
    tweet_url = data.get("tweet_url", "")
    if not tweet_url:
        return {"error": "Please provide a tweet URL"}
    content = data.get("content", "")
    error = _validate_content(content, "Reply")
    if error:
        return {"error": error}

    async def action(page: Page) -> dict:
        nav_err = await _navigate_to_tweet(page, tweet_url)
        if nav_err:
            return {"error": nav_err}

        tweet = page.locator(_SEL["tweet_article"]).first
        reply_btn = tweet.locator(_SEL["reply_button"])
        await reply_btn.wait_for(timeout=_TIMEOUTS["element"])
        await reply_btn.click()
        await page.wait_for_timeout(int(_TIMEOUTS["after_click"] * 1.5))

        dialog = page.locator(_SEL["modal"])
        await dialog.wait_for(timeout=_TIMEOUTS["element"])

        reply_input = dialog.locator(_SEL["tweet_input"])
        await reply_input.wait_for(timeout=_TIMEOUTS["element"])
        await reply_input.click()
        await page.wait_for_timeout(_TIMEOUTS["after_click"] // 2)
        await reply_input.fill(content)
        await page.wait_for_timeout(_TIMEOUTS["after_fill"])

        submit_btn = dialog.locator(_SEL["modal_submit"])
        await submit_btn.wait_for(timeout=_TIMEOUTS["element"])
        if await submit_btn.get_attribute("aria-disabled") == "true":
            return {"error": "Submit button disabled. Content may be empty or exceed limit."}

        await submit_btn.click()
        await page.wait_for_timeout(_TIMEOUTS["after_submit"])

        preview = content[:50] + ("..." if len(content) > 50 else "")
        return {"result": {"status": "ok", "message": f"Reply posted: {preview}"}}

    try:
        return await _with_browser(action)
    except Exception as exc:
        logger.error("X reply failed", error=str(exc))
        return {"error": str(exc)}


async def _handle_x_retweet(data: dict) -> dict:
    """Retweet a tweet on X (Twitter)."""
    tweet_url = data.get("tweet_url", "")
    if not tweet_url:
        return {"error": "Please provide a tweet URL"}

    async def action(page: Page) -> dict:
        nav_err = await _navigate_to_tweet(page, tweet_url)
        if nav_err:
            return {"error": nav_err}

        tweet = page.locator(_SEL["tweet_article"]).first

        if await _is_visible(tweet.locator(_SEL["unretweet"])):
            return {"result": {"status": "ok", "message": "Tweet already retweeted"}}

        rt_btn = tweet.locator(_SEL["retweet"])
        await rt_btn.wait_for(timeout=_TIMEOUTS["element"])
        await rt_btn.click()
        await page.wait_for_timeout(_TIMEOUTS["after_click"])

        confirm = page.locator(_SEL["retweet_confirm"])
        await confirm.wait_for(timeout=_TIMEOUTS["element"])
        await confirm.click()
        await page.wait_for_timeout(_TIMEOUTS["after_click"] * 2)

        if await _is_visible(tweet.locator(_SEL["unretweet"])):
            return {"result": {"status": "ok", "message": "Retweet successful"}}

        return {
            "result": {
                "status": "ok",
                "message": "Retweet action completed but could not verify success",
            }
        }

    try:
        return await _with_browser(action)
    except Exception as exc:
        logger.error("X retweet failed", error=str(exc))
        return {"error": str(exc)}


async def _handle_x_quote(data: dict) -> dict:
    """Quote tweet with a comment on X (Twitter)."""
    tweet_url = data.get("tweet_url", "")
    if not tweet_url:
        return {"error": "Please provide a tweet URL"}
    comment = data.get("comment", "")
    error = _validate_content(comment, "Comment")
    if error:
        return {"error": error}

    async def action(page: Page) -> dict:
        nav_err = await _navigate_to_tweet(page, tweet_url)
        if nav_err:
            return {"error": nav_err}

        tweet = page.locator(_SEL["tweet_article"]).first
        rt_btn = tweet.locator(_SEL["retweet"])
        await rt_btn.wait_for(timeout=_TIMEOUTS["element"])
        await rt_btn.click()
        await page.wait_for_timeout(_TIMEOUTS["after_click"])

        quote_option = page.get_by_role("menuitem").filter(
            has_text=re.compile(r"Quote", re.IGNORECASE),
        )
        await quote_option.wait_for(timeout=_TIMEOUTS["element"])
        await quote_option.click()
        await page.wait_for_timeout(int(_TIMEOUTS["after_click"] * 1.5))

        dialog = page.locator(_SEL["modal"])
        await dialog.wait_for(timeout=_TIMEOUTS["element"])

        quote_input = dialog.locator(_SEL["tweet_input"])
        await quote_input.wait_for(timeout=_TIMEOUTS["element"])
        await quote_input.click()
        await page.wait_for_timeout(_TIMEOUTS["after_click"] // 2)
        await quote_input.fill(comment)
        await page.wait_for_timeout(_TIMEOUTS["after_fill"])

        submit_btn = dialog.locator(_SEL["modal_submit"])
        await submit_btn.wait_for(timeout=_TIMEOUTS["element"])
        if await submit_btn.get_attribute("aria-disabled") == "true":
            return {"error": "Submit button disabled. Content may be empty or exceed limit."}

        await submit_btn.click()
        await page.wait_for_timeout(_TIMEOUTS["after_submit"])

        preview = comment[:50] + ("..." if len(comment) > 50 else "")
        return {"result": {"status": "ok", "message": f"Quote tweet posted: {preview}"}}

    try:
        return await _with_browser(action)
    except Exception as exc:
        logger.error("X quote failed", error=str(exc))
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


# Run at import time (same behavior as the old standalone script)
_check_system_deps()


class XIntegrationPlugin:
    @hookimpl
    def pynchy_service_handler(self) -> dict[str, Any]:
        return {
            "tools": {
                "setup_x_session": _handle_setup_x_session,
                "x_post": _handle_x_post,
                "x_like": _handle_x_like,
                "x_reply": _handle_x_reply,
                "x_retweet": _handle_x_retweet,
                "x_quote": _handle_x_quote,
            },
        }
