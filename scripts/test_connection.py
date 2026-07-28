"""
Quick end-to-end test: opens browser for Okta login, then fetches your courses.
Run with:  python scripts/test_connection.py
"""
import asyncio
import json
import sys
sys.path.insert(0, "src")

from blackboard_mcp.api import BlackboardClient
from blackboard_mcp.tools.courses import list_courses
from blackboard_mcp.tools.content import list_content


async def main():
    client = BlackboardClient()

    print("Step 1: Authenticating...")
    print("  A browser window will open — log in with your UQ credentials + Okta 2FA.")
    print("  The window closes automatically once you're logged in.\n")

    try:
        me = await client.get_me()
        print(f"  Logged in as: {me.get('name', {}).get('given', '')} {me.get('name', {}).get('family', '')} ({me.get('userName', '')})\n")
    except Exception as e:
        print(f"  Auth check failed: {e}")
        return

    print("Step 2: Fetching your enrolled courses...")
    try:
        courses = await list_courses(client)
        if not courses:
            print("  No courses found (or none currently available).")
        else:
            print(f"  Found {len(courses)} courses:\n")
            for c in courses:
                print(f"    [{c['display_id']}] {c['name']}")
                if c.get('term'):
                    print(f"         Term: {c['term']}")
    except Exception as e:
        print(f"  Failed to fetch courses: {e}")
        return

    if courses:
        first = courses[0]
        print(f"\nStep 3: Fetching content for '{first['name']}'...")
        try:
            items = await list_content(client, first["id"])
            print(f"  Top-level items ({len(items)}):")
            for item in items[:10]:
                icon = "📁" if item.get("has_children") else "📄"
                print(f"    {icon} {item['title']}")
            if len(items) > 10:
                print(f"    ... and {len(items) - 10} more")
        except Exception as e:
            print(f"  Failed to fetch content: {e}")

    await client.aclose()
    print("\nAll tests passed. MCP server is ready to use.")


if __name__ == "__main__":
    asyncio.run(main())
