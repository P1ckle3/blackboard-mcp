"""
Debug script: opens browser, auto-fills credentials, then shows exactly what
cookies are captured and what the Blackboard API returns.
Run: python scripts/debug_auth.py
"""
import asyncio
import json
import sys
sys.path.insert(0, "src")

import httpx
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

BB_HOST = "https://learn.uq.edu.au"
API_ME   = f"{BB_HOST}/learn/api/public/v1/users/me"

from blackboard_mcp.auth import _load_credentials, save_credentials, CREDS_FILE


async def main():
    username, password = _load_credentials()
    print(f"Credentials loaded: username={username!r}, password={'***' if password else None}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        print(f"Navigating to {BB_HOST} ...")
        await page.goto(BB_HOST)

        # Auto-fill username
        USER_SELS = ['input[name="identifier"]', 'input[name="username"]', 'input[type="email"]', "#username", "#okta-signin-username"]
        try:
            await page.wait_for_selector(", ".join(USER_SELS), timeout=12000)
            for sel in USER_SELS:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.fill(username)
                    await el.press("Enter")
                    print(f"Username filled via {sel}")
                    break
        except PWTimeout:
            print("No username field found — fill manually.")

        await asyncio.sleep(2)

        # Auto-fill password
        PW_SELS = ['input[type="password"]', 'input[name="password"]', "#okta-signin-password"]
        try:
            await page.wait_for_selector(", ".join(PW_SELS), timeout=10000)
            for sel in PW_SELS:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.fill(password)
                    await el.press("Enter")
                    print("Password filled — approve Okta on your phone now.")
                    break
        except PWTimeout:
            print("No password field — fill manually.")

        print("\nWaiting 30 seconds for you to approve Okta and land on Blackboard...")
        print("(watching URL changes)\n")

        for i in range(15):
            await asyncio.sleep(2)
            current_url = page.url
            print(f"  [{i*2+2}s] URL: {current_url}")

            raw = await context.cookies()  # ALL cookies, no filter
            bb_cookies = {c["name"]: c["value"] for c in raw if "uq.edu.au" in c.get("domain", "")}
            print(f"       UQ cookies so far: {list(bb_cookies.keys())}")

            if bb_cookies:
                async with httpx.AsyncClient(cookies=bb_cookies, follow_redirects=True) as client:
                    resp = await client.get(API_ME)
                    print(f"       API /users/me → HTTP {resp.status_code}")
                    if resp.status_code == 200:
                        data = resp.json()
                        print(f"\n  SUCCESS! Logged in as: {data.get('name', {}).get('given')} {data.get('name', {}).get('family')}")
                        from blackboard_mcp.auth import _save_cookies
                        _save_cookies(bb_cookies)
                        print("  Session saved.")
                        await browser.close()
                        return
                    elif resp.status_code != 401:
                        print(f"       Response body: {resp.text[:200]}")

        print("\nDid not detect successful login in 30s.")
        print("Final URL:", page.url)
        raw = await context.cookies()
        print("All cookies:", json.dumps([{"name": c["name"], "domain": c["domain"]} for c in raw], indent=2))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
