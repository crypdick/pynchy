"""Handler functions for the six X (Twitter) service tools."""

from __future__ import annotations

import contextlib
import re
import subprocess  # noqa: S404 - trusted display helper handles and beartype runtime annotation binding.
from typing import Any, cast

from pynchy.logger import logger
from pynchy.plugins.integrations._service import service_tool
from pynchy.plugins.integrations.browser import (
    cleanup_lock_files,
    has_display,
    profile_dir,
    stop_procs,
)
from pynchy.plugins.integrations.x_integration._browser import (
    SEL,
    TIMEOUTS,
    check_login,
    is_visible,
    launch_kwargs,
    navigate_to_tweet,
    validate_content,
    with_browser,
)
from pynchy.plugins.integrations.x_integration._contracts import (
    XLocator,
    XPage,
)
from pynchy.plugins.integrations.x_integration._display import ensure_xvfb, start_vnc_layer


async def handle_setup_x_session(data: dict[str, Any]) -> dict[str, Any]:
    """Launch a headed browser for manual X login. Saves the session."""
    timeout_seconds = data.get("timeout_seconds", 120)
    vnc_procs: list[subprocess.Popen[bytes]] = []
    novnc_url: str | None = None

    try:
        need_vnc = not has_display()
        ensure_xvfb()
        if need_vnc:
            vnc_procs, novnc_url = start_vnc_layer()
        return await _run_x_session_setup(timeout_seconds, novnc_url)
    except Exception as exc:  # noqa: BLE001 - X session setup failures are surfaced to the caller.
        logger.error("X session setup failed", error=str(exc))
        return {"error": str(exc)}

    finally:
        stop_procs(vnc_procs)


async def _run_x_session_setup(timeout_seconds: int, novnc_url: str | None) -> dict[str, Any]:
    from playwright.async_api import (  # noqa: PLC0415 - optional browser automation dependency.
        async_playwright,
    )

    x_profile = profile_dir("x")
    cleanup_lock_files(x_profile)

    async with async_playwright() as pw, contextlib.AsyncExitStack() as stack:
        context = await pw.chromium.launch_persistent_context(
            **launch_kwargs(x_profile),
        )
        stack.push_async_callback(context.close)
        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto(
            "https://x.com/login",
            timeout=TIMEOUTS["navigation"],
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(TIMEOUTS["page_load"])

        # Already logged in?
        if await is_visible(cast("XLocator", page.locator(SEL["account_switcher"]))):
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
                SEL["account_switcher"],
                timeout=timeout_seconds * 1000,
            )
        except Exception:  # noqa: BLE001  # allow: exception-handling - timeout becomes a caller-facing error.
            return {
                "error": (
                    f"Login not completed within {timeout_seconds}s. "
                    "Try again with a longer timeout."
                )
            }

    result = {
        "status": "ok",
        "profile_dir": str(x_profile),
        "message": "X session saved. Future tool calls will use this session.",
    }
    if novnc_url:
        result["novnc_url"] = novnc_url
    return {"result": result}


@service_tool
async def handle_x_post(data: dict[str, Any]) -> dict[str, Any]:
    """Post a tweet on X (Twitter)."""
    content = data.get("content", "")
    error = validate_content(content)
    if error:
        return {"error": error}

    async def action(page: XPage) -> dict[str, Any]:
        await page.goto(
            "https://x.com/home",
            timeout=TIMEOUTS["navigation"],
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(TIMEOUTS["page_load"])

        login_err = await check_login(page)
        if login_err:
            return {"error": login_err}

        tweet_input = page.locator(SEL["tweet_input"])
        await tweet_input.wait_for(timeout=TIMEOUTS["element"] * 2)
        await tweet_input.click()
        await page.wait_for_timeout(TIMEOUTS["after_click"] // 2)
        await tweet_input.fill(content)
        await page.wait_for_timeout(TIMEOUTS["after_fill"])

        post_btn = page.locator(SEL["post_button"])
        await post_btn.wait_for(timeout=TIMEOUTS["element"])
        if await post_btn.get_attribute("aria-disabled") == "true":
            return {"error": "Post button disabled. Content may be empty or exceed limit."}

        await post_btn.click()
        await page.wait_for_timeout(TIMEOUTS["after_submit"])

        preview = content[:50] + ("..." if len(content) > 50 else "")
        return {"result": {"status": "ok", "message": f"Tweet posted: {preview}"}}

    return await with_browser(action)


@service_tool
async def handle_x_like(data: dict[str, Any]) -> dict[str, Any]:
    """Like a tweet on X (Twitter)."""
    tweet_url = data.get("tweet_url", "")
    if not tweet_url:
        return {"error": "Please provide a tweet URL"}

    async def action(page: XPage) -> dict[str, Any]:
        nav_err = await navigate_to_tweet(page, tweet_url)
        if nav_err:
            return {"error": nav_err}

        tweet = page.locator(SEL["tweet_article"]).first

        if await is_visible(tweet.locator(SEL["unlike"])):
            return {"result": {"status": "ok", "message": "Tweet already liked"}}

        like_btn = tweet.locator(SEL["like"])
        await like_btn.wait_for(timeout=TIMEOUTS["element"])
        await like_btn.click()
        await page.wait_for_timeout(TIMEOUTS["after_click"])

        if await is_visible(tweet.locator(SEL["unlike"])):
            return {"result": {"status": "ok", "message": "Like successful"}}

        return {
            "result": {
                "status": "ok",
                "message": "Like action completed but could not verify success",
            }
        }

    return await with_browser(action)


@service_tool
async def handle_x_reply(data: dict[str, Any]) -> dict[str, Any]:
    """Reply to a tweet on X (Twitter)."""
    tweet_url = data.get("tweet_url", "")
    if not tweet_url:
        return {"error": "Please provide a tweet URL"}
    content = data.get("content", "")
    error = validate_content(content, "Reply")
    if error:
        return {"error": error}

    async def action(page: XPage) -> dict[str, Any]:
        nav_err = await navigate_to_tweet(page, tweet_url)
        if nav_err:
            return {"error": nav_err}

        tweet = page.locator(SEL["tweet_article"]).first
        reply_btn = tweet.locator(SEL["reply_button"])
        await reply_btn.wait_for(timeout=TIMEOUTS["element"])
        await reply_btn.click()
        await page.wait_for_timeout(int(TIMEOUTS["after_click"] * 1.5))

        dialog = page.locator(SEL["modal"])
        await dialog.wait_for(timeout=TIMEOUTS["element"])

        reply_input = dialog.locator(SEL["tweet_input"])
        await reply_input.wait_for(timeout=TIMEOUTS["element"])
        await reply_input.click()
        await page.wait_for_timeout(TIMEOUTS["after_click"] // 2)
        await reply_input.fill(content)
        await page.wait_for_timeout(TIMEOUTS["after_fill"])

        submit_btn = dialog.locator(SEL["modal_submit"])
        await submit_btn.wait_for(timeout=TIMEOUTS["element"])
        if await submit_btn.get_attribute("aria-disabled") == "true":
            return {"error": "Submit button disabled. Content may be empty or exceed limit."}

        await submit_btn.click()
        await page.wait_for_timeout(TIMEOUTS["after_submit"])

        preview = content[:50] + ("..." if len(content) > 50 else "")
        return {"result": {"status": "ok", "message": f"Reply posted: {preview}"}}

    return await with_browser(action)


@service_tool
async def handle_x_retweet(data: dict[str, Any]) -> dict[str, Any]:
    """Retweet a tweet on X (Twitter)."""
    tweet_url = data.get("tweet_url", "")
    if not tweet_url:
        return {"error": "Please provide a tweet URL"}

    async def action(page: XPage) -> dict[str, Any]:
        nav_err = await navigate_to_tweet(page, tweet_url)
        if nav_err:
            return {"error": nav_err}

        tweet = page.locator(SEL["tweet_article"]).first

        if await is_visible(tweet.locator(SEL["unretweet"])):
            return {"result": {"status": "ok", "message": "Tweet already retweeted"}}

        rt_btn = tweet.locator(SEL["retweet"])
        await rt_btn.wait_for(timeout=TIMEOUTS["element"])
        await rt_btn.click()
        await page.wait_for_timeout(TIMEOUTS["after_click"])

        confirm = page.locator(SEL["retweet_confirm"])
        await confirm.wait_for(timeout=TIMEOUTS["element"])
        await confirm.click()
        await page.wait_for_timeout(TIMEOUTS["after_click"] * 2)

        if await is_visible(tweet.locator(SEL["unretweet"])):
            return {"result": {"status": "ok", "message": "Retweet successful"}}

        return {
            "result": {
                "status": "ok",
                "message": "Retweet action completed but could not verify success",
            }
        }

    return await with_browser(action)


@service_tool
async def handle_x_quote(data: dict[str, Any]) -> dict[str, Any]:
    """Quote tweet with a comment on X (Twitter)."""
    tweet_url = data.get("tweet_url", "")
    if not tweet_url:
        return {"error": "Please provide a tweet URL"}
    comment = data.get("comment", "")
    error = validate_content(comment, "Comment")
    if error:
        return {"error": error}

    async def action(page: XPage) -> dict[str, Any]:
        nav_err = await navigate_to_tweet(page, tweet_url)
        if nav_err:
            return {"error": nav_err}

        tweet = page.locator(SEL["tweet_article"]).first
        rt_btn = tweet.locator(SEL["retweet"])
        await rt_btn.wait_for(timeout=TIMEOUTS["element"])
        await rt_btn.click()
        await page.wait_for_timeout(TIMEOUTS["after_click"])

        quote_option = page.get_by_role("menuitem").filter(
            has_text=re.compile(r"Quote", re.IGNORECASE),
        )
        await quote_option.wait_for(timeout=TIMEOUTS["element"])
        await quote_option.click()
        await page.wait_for_timeout(int(TIMEOUTS["after_click"] * 1.5))

        dialog = page.locator(SEL["modal"])
        await dialog.wait_for(timeout=TIMEOUTS["element"])

        quote_input = dialog.locator(SEL["tweet_input"])
        await quote_input.wait_for(timeout=TIMEOUTS["element"])
        await quote_input.click()
        await page.wait_for_timeout(TIMEOUTS["after_click"] // 2)
        await quote_input.fill(comment)
        await page.wait_for_timeout(TIMEOUTS["after_fill"])

        submit_btn = dialog.locator(SEL["modal_submit"])
        await submit_btn.wait_for(timeout=TIMEOUTS["element"])
        if await submit_btn.get_attribute("aria-disabled") == "true":
            return {"error": "Submit button disabled. Content may be empty or exceed limit."}

        await submit_btn.click()
        await page.wait_for_timeout(TIMEOUTS["after_submit"])

        preview = comment[:50] + ("..." if len(comment) > 50 else "")
        return {"result": {"status": "ok", "message": f"Quote tweet posted: {preview}"}}

    return await with_browser(action)
