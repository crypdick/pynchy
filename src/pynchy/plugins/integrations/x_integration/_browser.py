"""Playwright helpers, selectors, and browser-launch config for X actions."""

from __future__ import annotations

import re
from collections.abc import (
    Awaitable,
    Callable,
)
from pathlib import Path
from typing import Any, cast

from pynchy.plugins.integrations.browser import chrome_path, cleanup_lock_files, profile_dir
from pynchy.plugins.integrations.x_integration._contracts import (
    XLocator,
    XPage,
)
from pynchy.plugins.integrations.x_integration._display import ensure_xvfb

# X UI selectors (data-testid based).  These match X's React component
# test IDs and are the same ones the archived TS implementation used.
SEL = {
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

TIMEOUTS = {
    "navigation": 30_000,
    "element": 5_000,
    "after_click": 1_000,
    "after_fill": 1_000,
    "after_submit": 3_000,
    "page_load": 3_000,
}

TWEET_MAX = 280

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


async def is_visible(locator: XLocator) -> bool:
    """Check locator visibility without raising on detached elements."""
    try:
        return bool(await locator.is_visible())
    except Exception:  # noqa: BLE001  # allow: exception-handling - detached elements are treated as not visible.
        return False


def validate_content(content: str | None, label: str = "Tweet") -> str | None:
    """Validate tweet content.  Returns error string or None if valid."""
    if not content:
        return f"{label} content cannot be empty"
    if len(content) > TWEET_MAX:
        return f"{label} exceeds {TWEET_MAX} char limit (current: {len(content)})"
    return None


async def navigate_to_tweet(page: XPage, tweet_url: str) -> str | None:
    """Navigate to a tweet page.  Returns error message or None on success."""
    url = tweet_url.strip()
    if re.match(r"^\d+$", url):
        url = f"https://x.com/i/status/{url}"
    elif not url.startswith("http"):
        url = f"https://{url}"

    try:
        await page.goto(
            url,
            timeout=TIMEOUTS["navigation"],
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(TIMEOUTS["page_load"])
    except Exception as exc:  # noqa: BLE001  # allow: exception-handling - navigation errors are surfaced to the caller.
        return f"Navigation failed: {exc}"

    if not await is_visible(page.locator(SEL["tweet_article"]).first):
        return "Tweet not found. It may have been deleted or the URL is invalid."
    return None


def launch_kwargs(profile_path: Path) -> dict[str, Any]:
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


async def with_browser(
    fn: Callable[[XPage], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Run *fn(page)* inside a persistent browser context.

    Manages Xvfb display, lock-file cleanup, Playwright lifecycle.
    Used by action tools (``setup_x_session`` has its own VNC flow).
    """
    from playwright.async_api import (  # noqa: PLC0415 - optional browser automation dependency.
        async_playwright,
    )

    ensure_xvfb()
    x_profile = profile_dir("x")
    cleanup_lock_files(x_profile)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            **launch_kwargs(x_profile),
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            # Playwright narrows keyword parameters with Literal types, so its
            # Page does not structurally satisfy the smaller integration
            # protocol even though it provides every operation we consume.
            return await fn(cast("XPage", page))
        finally:
            await context.close()


async def check_login(page: XPage) -> str | None:
    """Return an error string if not logged in, None if OK."""
    if await is_visible(page.locator(SEL["account_switcher"])):
        return None
    if await is_visible(page.locator(SEL["login_input"])):
        return "X login expired. Run setup_x_session to re-authenticate."
    # Page may still be loading — don't fail yet
    return None
