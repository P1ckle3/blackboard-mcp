"""Tools: list_due_dates, assignment_status.

Both are built from gradebook columns rather than the calendar API:
/courses/{id}/calendarItems does not exist on this instance, and
/calendars/items rejects since/until and caps at a short default page. Every
item it does return is type=GradebookColumn anyway, so `grading.due` on the
columns is the same data from a source that paginates properly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..api import BlackboardClient
from .courses import list_courses, parse_ts

COURSE_CONCURRENCY = 4
COLUMN_CONCURRENCY = 6


def _status(record: dict, due: Optional[datetime], now: datetime) -> str:
    """Derive a student-facing state from the gradebook record and due date."""
    raw = (record or {}).get("status")
    score = (record or {}).get("score")
    if (record or {}).get("exempt"):
        return "exempt"
    if raw == "Graded" and score is not None:
        return "graded"
    if raw in ("NeedsGrading", "InProgress") or score is not None:
        return "submitted"
    if due and due < now:
        return "overdue"
    if due:
        return "upcoming"
    return "no_due_date"


async def _collect_course(
    client: BlackboardClient,
    course: dict,
    now: datetime,
    col_sem: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    """One row per gradebook column, with due date and my current status."""
    try:
        columns = await client.get_grade_columns(course["id"])
    except Exception:
        return []

    async def _one(col: dict) -> Optional[dict[str, Any]]:
        col_id = col.get("id")
        if not col_id:
            return None
        grading = col.get("grading") or {}
        due = parse_ts(grading.get("due"))

        record: dict = {}
        try:
            async with col_sem:
                record = await client.get_my_grade(course["id"], col_id)
        except Exception:
            record = {}

        return {
            "course_id": course["id"],
            "course": course.get("display_id") or course.get("name"),
            "course_name": course.get("name"),
            "column_id": col_id,
            "title": col.get("name"),
            "due": grading.get("due"),
            "days_until_due": round((due - now).total_seconds() / 86400, 1) if due else None,
            "points_possible": (col.get("score") or {}).get("possible"),
            "score": record.get("score"),
            "status": _status(record, due, now),
            "attempts_allowed": grading.get("attemptsAllowed"),
        }

    rows = await asyncio.gather(*(_one(c) for c in columns))
    return [r for r in rows if r]


async def _collect(
    client: BlackboardClient,
    course_id: Optional[str],
    current_only: bool,
) -> tuple[list[dict[str, Any]], datetime]:
    now = datetime.now(timezone.utc)
    courses = await list_courses(client, current_only=current_only and not course_id)
    if course_id:
        courses = [c for c in courses if c["id"] == course_id] or [
            {"id": course_id, "name": course_id, "display_id": course_id}
        ]

    col_sem = asyncio.Semaphore(COLUMN_CONCURRENCY)
    course_sem = asyncio.Semaphore(COURSE_CONCURRENCY)

    async def _bounded(course: dict) -> list[dict[str, Any]]:
        async with course_sem:
            return await _collect_course(client, course, now, col_sem)

    per_course = await asyncio.gather(*(_bounded(c) for c in courses))
    return [r for rows in per_course for r in rows], now


async def list_due_dates(
    client: BlackboardClient,
    course_id: Optional[str] = None,
    days_ahead: Optional[int] = None,
    include_past: bool = False,
    current_only: bool = True,
) -> list[dict[str, Any]]:
    """Upcoming assessment due dates, soonest first.

    Defaults to current-term courses only; set current_only=false to include
    past enrolments. Items with no due date are omitted.
    """
    rows, now = await _collect(client, course_id, current_only)
    rows = [r for r in rows if r["due"]]

    if not include_past:
        rows = [r for r in rows if (r["days_until_due"] or 0) >= 0]
    if days_ahead is not None:
        cutoff = now + timedelta(days=days_ahead)
        rows = [r for r in rows if (parse_ts(r["due"]) or now) <= cutoff]

    rows.sort(key=lambda r: r["due"])
    return rows


async def assignment_status(
    client: BlackboardClient,
    course_id: Optional[str] = None,
    current_only: bool = True,
) -> dict[str, Any]:
    """Every assessment grouped by state, with a summary count per state."""
    rows, _ = await _collect(client, course_id, current_only)
    rows.sort(key=lambda r: (r["due"] or "9999", r["title"] or ""))

    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(r["status"], []).append(r)

    graded = [r for r in rows if r["status"] == "graded"]
    earned = sum(r["score"] or 0 for r in graded)
    possible = sum(r["points_possible"] or 0 for r in graded)

    return {
        "summary": {k: len(v) for k, v in sorted(grouped.items())},
        "graded_points": {
            "earned": round(earned, 2),
            "possible": round(possible, 2),
            "percent": round(100 * earned / possible, 1) if possible else None,
        },
        "by_status": grouped,
    }
