"""
Run this script once to discover the exact Okta login URL for UQ Blackboard.
It opens a browser, intercepts the SAML redirect, prints the start_url and
target_url needed for blackboard_mcp and the BlackboardSync institution JSON.

Usage:
    python scripts/discover_uq_login_url.py
"""

import asyncio


async def main() -> None:
    from playwright.async_api import async_playwright

    LEARN_HOST = "https://learn.uq.edu.au"
    start_url: list[str] = []
    target_url: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        def on_request(request):
            url = request.url
            # Capture first redirect away from learn.uq.edu.au (the SAML endpoint)
            if not start_url and "learn.uq.edu.au" not in url and "okta" not in url.lower():
                pass
            # Capture the initial Blackboard SAML initiation URL
            if not start_url and "learn.uq.edu.au" in url and "saml" in url.lower():
                start_url.append(url)
                print(f"\n[start_url] {url}")

        def on_response(response):
            url = response.url
            # When we land back on learn.uq.edu.au after Okta, capture that URL
            if "learn.uq.edu.au" in url and response.status in (200, 302) and not target_url:
                if "webapps" in url or "portal" in url:
                    target_url.append(url)
                    print(f"[target_url] {url}")

        page.on("request", on_request)
        page.on("response", on_response)

        print("Opening UQ Blackboard — please log in with your Okta credentials and complete 2FA.")
        print("This window will close automatically once you are logged in.\n")
        await page.goto(LEARN_HOST)

        # Wait for successful landing on the portal
        try:
            await page.wait_for_url("**/learn.uq.edu.au/webapps/**", timeout=300_000)
        except Exception:
            # Fallback: wait for URL to contain learn.uq.edu.au and not be a redirect loop
            await asyncio.sleep(5)

        if not target_url:
            target_url.append(page.url)
            print(f"[target_url fallback] {page.url}")

        await browser.close()

    print("\n--- Copy these into your institution JSON ---")
    print(f'  "start_url": "{start_url[0] if start_url else "COULD NOT DETECT — check browser network tab"}"')
    print(f'  "target_url": "{target_url[0] if target_url else "COULD NOT DETECT"}"')
    print("\nFull institution JSON entry for BlackboardSync:")
    print("""{
  "name": "University of Queensland",
  "short_name": "UQ",
  "country": "AU",
  "login": {""")
    print(f'    "start_url": "{start_url[0] if start_url else "REPLACE_ME"}",')
    print(f'    "target_url": "{target_url[0] if target_url else "REPLACE_ME"}"')
    print("""  },
  "api_url": "https://learn.uq.edu.au"
}""")


if __name__ == "__main__":
    asyncio.run(main())
