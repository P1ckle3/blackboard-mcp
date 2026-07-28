"""Tools: list_courses, get_me."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from ..api import BlackboardClient


def parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _term_index(terms: list[dict]) -> dict[str, dict]:
    """Map term id -> {name, start, end}.

    expand=course.term returns null on this instance, so terms are resolved
    from /terms separately.
    """
    index = {}
    for t in terms:
        duration = (t.get("availability") or {}).get("duration") or {}
        index[t.get("id")] = {
            "name": t.get("name"),
            "start": parse_ts(duration.get("start")),
            "end": parse_ts(duration.get("end")),
        }
    return index


async def list_courses(
    client: BlackboardClient,
    current_only: bool = False,
) -> list[dict[str, Any]]:
    """List enrolled courses, annotated with term name and whether it's current.

    A student accumulates years of dead enrolments; current_only keeps just
    the courses whose term date range contains today.
    """
    memberships = await client.get_courses()

    try:
        terms = _term_index(await client.get_terms())
    except Exception:
        terms = {}

    now = datetime.now(timezone.utc)
    courses = []
    for m in memberships:
        co = m.get("course") or {}
        term = terms.get(co.get("termId")) or {}
        start, end = term.get("start"), term.get("end")
        is_current = bool(start and end and start <= now <= end)
        courses.append({
            "id": co.get("id") or m.get("courseId"),
            "name": co.get("name", ""),
            "display_id": co.get("courseId", ""),
            "description": co.get("description", ""),
            "available": (m.get("availability") or {}).get("available"),
            "role": m.get("courseRoleId", ""),
            "url": co.get("externalAccessUrl", ""),
            "term": term.get("name"),
            "is_current": is_current,
            "last_accessed": m.get("lastAccessed"),
        })

    if current_only:
        courses = [c for c in courses if c["is_current"]]

    # Most recently accessed first, then stable-sort current courses to the top
    courses.sort(key=lambda c: c.get("last_accessed") or "", reverse=True)
    courses.sort(key=lambda c: not c["is_current"])
    return courses


async def get_me(client: BlackboardClient) -> dict[str, Any]:
    """Current user's Blackboard profile."""
    me = await client.get_me()
    name = me.get("name") or {}
    return {
        "id": me.get("id"),
        "username": me.get("userName"),
        "given_name": name.get("given"),
        "family_name": name.get("family"),
        "email": (me.get("contact") or {}).get("email"),
        "student_id": me.get("studentId"),
    }
