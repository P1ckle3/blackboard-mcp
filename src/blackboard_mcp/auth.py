"""Okta SSO authentication for UQ Blackboard via Playwright browser automation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from functools import partial
from pathlib import Path
from typing import Optional

_eprint = partial(print, file=sys.stderr, flush=True)

import httpx
import keyring

logger = logging.getLogger(__name__)

SESSION_FILE = Path.home() / ".blackboard_mcp" / "session.json"
CREDS_FILE   = Path.home() / ".blackboard_mcp" / "credentials.json"
BB_HOST      = "https://learn.uq.edu.au"
BB_HOSTNAME  = "learn.uq.edu.au"
AUTH_CHECK_URL = f"{BB_HOST}/learn/api/public/v1/users/me"
KEYRING_SERVICE = "blackboard_mcp"

# Paths served by learn.uq.edu.au that are still part of the SSO handshake.
# Checked against the PATH only - matching the whole URL let an encoded
# ?returnUrl=...sso.uq.edu.au... make a legitimate page look like SSO forever.
_AUTH_PATHS = ("/auth-saml/", "/webapps/login", "/idp/")

LOGIN_HINT = (
    "Not authenticated. Run:  python -m blackboard_mcp --login\n"
    "A browser opens for UQ SSO; approve the Okta push on your phone."
)


class AuthRequired(RuntimeError):
    """No valid cached session and interactive login was not permitted."""

    def __init__(self, msg: str = LOGIN_HINT) -> None:
        super().__init__(msg)


def _on_bb(url: str) -> bool:
    """True if the URL is on UQ Blackboard proper (post-login), not on SSO."""
    try:
        u = httpx.URL(url)
    except Exception:
        return False
    if (u.host or "").lower() != BB_HOSTNAME:
        return False
    return not any(p in u.path.lower() for p in _AUTH_PATHS)


# ---------------------------------------------------------------------------
# Credentials - password lives in the OS keyring, never on disk
# ---------------------------------------------------------------------------

def _migrate_plaintext() -> None:
    """One-shot: move a legacy cleartext password into the OS keyring."""
    if not CREDS_FILE.exists():
        return
    try:
        d = json.loads(CREDS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    password = d.get("password")
    if not password:
        return
    username = d.get("username")
    try:
        if username:
            keyring.set_password(KEYRING_SERVICE, username, password)
        CREDS_FILE.write_text(
            json.dumps({"username": username}, indent=2), encoding="utf-8"
        )
        _eprint("[Auth] Migrated cleartext password into the OS keyring. "
                "Rotate your UQ password - it was stored unprotected on disk.")
    except Exception as e:
        _eprint(f"[Auth] Keyring migration failed: {e}")


def _load_credentials() -> tuple[Optional[str], Optional[str]]:
    _migrate_plaintext()
    if not CREDS_FILE.exists():
        return None, None
    try:
        username = json.loads(CREDS_FILE.read_text(encoding="utf-8")).get("username")
    except Exception:
        return None, None
    if not username:
        return None, None
    try:
        return username, keyring.get_password(KEYRING_SERVICE, username)
    except Exception as e:
        _eprint(f"[Auth] Keyring unavailable: {e}")
        return username, None


def save_credentials(username: str, password: str) -> None:
    """Store the password in the OS keyring; only the username hits disk."""
    keyring.set_password(KEYRING_SERVICE, username, password)
    CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CREDS_FILE.write_text(
        json.dumps({"username": username}, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Cookie cache
# ---------------------------------------------------------------------------

def _load_cached_cookies() -> Optional[dict[str, str]]:
    if SESSION_FILE.exists():
        try:
            return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _save_cookies(cookies: dict[str, str]) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    _eprint(f"[Auth] Session saved ({len(cookies)} cookies).")


async def _test_api(cookies: dict[str, str]) -> bool:
    """Test whether cookies give access to the Blackboard REST API."""
    from .api import bb_cookie_jar  # local import: api imports this module

    headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
    try:
        async with httpx.AsyncClient(
            cookies=bb_cookie_jar(cookies), follow_redirects=True, headers=headers
        ) as c:
            r = await c.get(AUTH_CHECK_URL, timeout=10)
            _eprint(f"[Auth] API check: HTTP {r.status_code}")
            if r.status_code != 200:
                logger.debug("Auth check response: %s", r.text[:200])
            return r.status_code == 200
    except Exception as e:
        _eprint(f"[Auth] API check error: {e}")
        return False


# ---------------------------------------------------------------------------
# Browser login
# ---------------------------------------------------------------------------

async def _autofill(page) -> None:
    """Fill username/password - silent on failure."""
    from playwright.async_api import Error as PWError

    username, password = _load_credentials()
    if not (username and password):
        return
    USER = ['input[name="identifier"]', 'input[name="username"]',
            'input[type="email"]', "#username", "#okta-signin-username"]
    PW   = ['input[type="password"]', 'input[name="password"]', "#okta-signin-password"]
    # PWError covers TargetClosedError, which is a SIBLING of TimeoutError, not a
    # subclass - closing the browser mid-autofill used to raise it uncaught.
    try:
        await page.wait_for_selector(", ".join(USER), timeout=12_000)
        for sel in USER:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(username)
                await el.press("Enter")
                _eprint("[Auth] Username filled.")
                break
    except PWError:
        return
    await asyncio.sleep(1.5)
    try:
        await page.wait_for_selector(", ".join(PW), timeout=10_000)
        for sel in PW:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(password)
                await el.press("Enter")
                _eprint("[Auth] Password filled.")
                break
    except PWError:
        pass

    # Click "Send Push" / "Approve via push notification" button if present
    await asyncio.sleep(2)
    PUSH_SELS = [
        'input[value*="Push"]',
        'a[data-se="send-push"]',
        '[data-se="send-push"]',
        'input[value*="push" i]',
        'button:has-text("push")',
        'a:has-text("push")',
        '[class*="push"]',
    ]
    for sel in PUSH_SELS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                _eprint(f"[Auth] Clicked push notification button ({sel}) - approve on your phone.")
                break
        except PWError:
            continue
    else:
        _eprint("[Auth] No push button found - approve on your phone or check the browser.")


async def _browser_login(timeout_s: int = 300) -> dict[str, str]:
    """Open browser, auto-fill creds, wait for redirect back to Blackboard."""
    from playwright.async_api import async_playwright

    _eprint("\n[Blackboard MCP] Opening browser for UQ login...")

    cookie_dict: dict[str, str] = {}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context  = await browser.new_context()
            page     = await context.new_page()

            await page.goto(BB_HOST)
            fill_task = asyncio.ensure_future(_autofill(page))

            _eprint("[Blackboard MCP] Waiting for login... (approve Okta then come back here)")

            # Poll URL every 2 seconds until we're on Blackboard (not Okta/SSO)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_s
            while loop.time() < deadline:
                await asyncio.sleep(2)
                current = page.url
                if _on_bb(current):
                    _eprint(f"[Auth] Redirected to Blackboard: {current}")
                    await asyncio.sleep(2)  # let page settle + set final cookies
                    break
            else:
                _eprint(f"[Blackboard MCP] {timeout_s}s timeout - capturing cookies anyway.")

            # Stop autofill before tearing down the browser, or it raises
            # TargetClosedError into a task nobody awaits.
            fill_task.cancel()
            with contextlib.suppress(BaseException):
                await fill_task

            # Capture all UQ cookies
            raw = await context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in raw
                           if "uq.edu.au" in c.get("domain", "")}
            _eprint(f"[Auth] Captured {len(cookie_dict)} cookies.")
            await browser.close()

    except Exception as e:
        _eprint(f"[Auth] Browser error: {e}")

    return cookie_dict


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_authenticated_cookies(interactive: bool = False) -> dict[str, str]:
    """Return valid Blackboard cookies.

    Non-interactive by default: a tool call must never block the MCP stdio loop
    for minutes waiting on a human to tap a phone. Raises AuthRequired instead.
    """
    cached = _load_cached_cookies()
    if cached and await _test_api(cached):
        logger.debug("Using cached session.")
        return cached

    if not interactive:
        if cached:
            _eprint("[Blackboard MCP] Cached session expired.")
        raise AuthRequired()

    if cached:
        _eprint("[Blackboard MCP] Cached session invalid - re-authenticating.")

    cookies = await _browser_login()

    if not cookies:
        raise AuthRequired("Login failed - no cookies captured. "
                           "Re-run: python -m blackboard_mcp --login")

    _save_cookies(cookies)

    if await _test_api(cookies):
        _eprint("[Auth] REST API confirmed working.")
    else:
        _eprint("[Auth] Cookies saved but REST API returned non-200 - will try anyway.")

    return cookies


async def login() -> bool:
    """Interactive login entry point for `python -m blackboard_mcp --login`."""
    try:
        await get_authenticated_cookies(interactive=True)
    except AuthRequired as e:
        _eprint(f"[Auth] {e}")
        return False
    _eprint("[Auth] Logged in. You can now start the MCP server.")
    return True


def clear_session() -> None:
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
        _eprint("Session cache cleared.")
