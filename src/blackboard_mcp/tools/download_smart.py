"""
Smart content-type-aware recursive downloader for Blackboard.

Inspired by the BlackboardSync project's content handler dispatch system:
  - x-bb-file / x-bb-document / x-bb-assignment  → download attachments
  - x-bb-folder / x-bb-lesson                    → recurse into children
  - x-bb-externallink                             → save as .url shortcut
  - x-bb-blti-link / x-bb-bltiplacement-*        → save LTI tool URL
  - body HTML                                     → extract + download embedded links

Downloads are incremental: a file already on disk whose size matches the
server's is skipped, so re-running is a cheap sync rather than a full refetch.
"""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from ..api import BB_HOST, BB_HOSTNAME, BlackboardAPIError, BlackboardClient
from ..paths import confine, safe_name

# Bounded fan-out: fast enough to cut a semester sync from minutes to seconds,
# polite enough not to look like a scrape to a shared university server.
DEFAULT_CONCURRENCY = 6

# Content handler id prefixes / exact matches
_FILE_HANDLERS     = {"resource/x-bb-file", "resource/x-bb-document",
                      "resource/x-bb-assignment"}
_FOLDER_HANDLERS   = {"resource/x-bb-folder", "resource/x-bb-lesson"}
_LINK_HANDLER      = "resource/x-bb-externallink"
_BLTI_HANDLER      = "resource/x-bb-blti-link"
_BLTI_PLACEMENT_PFX = "resource/x-bb-bltiplacement-"

# Blackboard emits literal "undefined" in some Ultra embedded hrefs
_JUNK_NAMES = {"", "undefined", "null", "ultradocumentbody"}


class _Ctx:
    """Shared state for one download run."""

    def __init__(self, sem: asyncio.Semaphore, force: bool) -> None:
        self.sem = sem
        self.force = force
        self.results: list[dict] = []
        self.errors: list[dict] = []
        # Depth cuts are a deliberate cap, not a failure — but they must still
        # be reported, or a truncated tree looks like a complete one.
        self.truncated: list[str] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fix_extension(filename: str, mime_type: str) -> str:
    """Add an extension when the filename has none.

    Only fills a gap — it never appends to a name that already has a suffix.
    Blackboard commonly reports OOXML files under legacy MIME ids, so appending
    there produced "slides.pptx.ppt", which Windows opens with the wrong handler.
    """
    if not mime_type or mime_type == "application/octet-stream":
        return filename
    if Path(filename).suffix:
        return filename
    candidates = mimetypes.guess_all_extensions(mime_type, strict=False)
    return filename + candidates[0] if candidates else filename


def _assign_names(names: list[str]) -> list[str]:
    """Disambiguate duplicate filenames within one item, deterministically.

    Deterministic matters for sync: a counter that depends on what already
    exists on disk would emit "x (1)", "x (2)" on every re-run.
    """
    out: list[str] = []
    seen: dict[str, int] = {}
    for n in names:
        s = safe_name(n)
        if s in seen:
            seen[s] += 1
            p = Path(s)
            s = f"{p.stem} ({seen[s]}){p.suffix}"
        else:
            seen[s] = 1
        out.append(s)
    return out


def _up_to_date(dest_file: Path, expected_size: Optional[int], force: bool) -> bool:
    """True if the local copy can be left alone."""
    if force or not dest_file.exists():
        return False
    actual = dest_file.stat().st_size
    if expected_size:
        return actual == expected_size
    return actual > 0     # no size advertised: presence is the best signal


def _extract_body_links(body_html: str) -> list[tuple[str, str]]:
    """
    Parse a content body HTML string and return (href, filename) tuples
    for all links/images that point back to the Blackboard host.
    """
    if not body_html:
        return []
    soup = BeautifulSoup(body_html, "html.parser")
    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    for tag, attr in [("a", "href"), ("img", "src")]:
        for el in soup.find_all(tag):
            uri: str = el.get(attr, "")
            if not uri or uri.startswith("#"):
                continue
            # urljoin (not string concat) so "//host/x", "mailto:", "images/x.png"
            # and "#anchor" resolve correctly instead of producing hosts like
            # "learn.uq.edu.auimages". Host must match EXACTLY: a substring test
            # accepted "learn.uq.edu.au.evil.com" and "?r=learn.uq.edu.au".
            full = urljoin(BB_HOST + "/", uri)
            parsed = urlparse(full)
            if parsed.scheme not in ("http", "https"):
                continue
            if (parsed.hostname or "").lower() != BB_HOSTNAME or full in seen:
                continue
            seen.add(full)
            filename = unquote(full.split("/")[-1].split("?")[0]).strip()
            if filename.lower() in _JUNK_NAMES:
                filename = "embedded"
            results.append((full, filename))

    return results


def _url_shortcut(url: str, title: str, dest_dir: Path, force: bool = False) -> dict:
    """Save a Windows .url shortcut and return a result dict."""
    # A newline in the server-supplied URL injects arbitrary INI keys. An
    # injected IconFile=\\attacker\share fires an SMB auth (leaking the NetNTLM
    # hash) as soon as Explorer renders the folder — no click required.
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or any(c in url for c in "\r\n"):
        raise ValueError(f"refusing to write shortcut for unsafe url: {url!r}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (safe_name(title) + ".url")
    body = f"[InternetShortcut]\nURL={url}\n"
    if not force and dest.exists():
        try:
            if dest.read_text(encoding="utf-8") == body:
                return {"saved": str(dest), "type": "skipped_unchanged", "url": url}
        except OSError:
            pass
    dest.write_text(body, encoding="utf-8")
    return {"saved": str(dest), "type": "url_shortcut", "url": url}


def _is_no_attachments_error(e: Exception) -> bool:
    """Ultra content items legitimately 400 with 'does not support attachments'."""
    if isinstance(e, BlackboardAPIError):
        return e.status == 400
    return "does not support" in str(e)


# ---------------------------------------------------------------------------
# Core recursive walker
# ---------------------------------------------------------------------------

async def _walk(
    client: BlackboardClient,
    course_id: str,
    content_id: str,
    dest: Path,
    depth: int,
    max_depth: int,
    ctx: _Ctx,
) -> None:
    if depth > max_depth:
        ctx.truncated.append(content_id)
        return

    # Fetch the item
    try:
        async with ctx.sem:
            item = await client.get_content_item(course_id, content_id)
    except Exception as e:
        ctx.errors.append({"content_id": content_id, "error": str(e)})
        return

    handler_obj  = item.get("contentHandler") or {}
    handler_id   = handler_obj.get("id", "") if isinstance(handler_obj, dict) else ""
    handler_url  = handler_obj.get("url", "")   if isinstance(handler_obj, dict) else ""
    title        = item.get("title", "untitled")
    body_html    = item.get("body", "") or ""
    has_children = item.get("hasChildren", False)

    item_dir = dest / safe_name(title)

    # ------------------------------------------------------------------ #
    # 1. FILE / DOCUMENT / ASSIGNMENT  →  download all attachments        #
    # ------------------------------------------------------------------ #
    if handler_id in _FILE_HANDLERS:
        # Fetched once here and passed down — this used to be requested twice
        # per item, once just to count and again to download.
        try:
            async with ctx.sem:
                attachments = await client.get_attachments(course_id, content_id)
        except Exception as e:
            attachments = []
            if not _is_no_attachments_error(e):
                ctx.errors.append({"title": title, "error": f"get_attachments: {e}"})

        # Single attachment → save into parent dir (avoid double-nesting when
        # the content title IS the filename)
        att_dest = dest if len(attachments) <= 1 else item_dir
        await _download_attachments(client, course_id, content_id,
                                    att_dest, title, attachments, ctx)
        if body_html:
            await _handle_body(client, body_html, item_dir, ctx)

    # ------------------------------------------------------------------ #
    # 2. FOLDER / LESSON  →  recurse into children                        #
    # ------------------------------------------------------------------ #
    elif handler_id in _FOLDER_HANDLERS or has_children:
        try:
            async with ctx.sem:
                children = await client.get_contents(course_id, content_id)
        except Exception as e:
            ctx.errors.append({"title": title, "error": str(e)})
            return

        item_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.gather(*(
            _walk(client, course_id, child["id"], item_dir,
                  depth + 1, max_depth, ctx)
            for child in children if child.get("id")
        ))

    # ------------------------------------------------------------------ #
    # 3 & 4. EXTERNAL LINK / LTI TOOL  →  save .url shortcut              #
    # ------------------------------------------------------------------ #
    elif handler_id in (_LINK_HANDLER, _BLTI_HANDLER) or \
            handler_id.startswith(_BLTI_PLACEMENT_PFX):
        # Keep the shortcut and any body content together in one place rather
        # than splitting them across a sibling file and directory.
        link_dest = item_dir if body_html else dest
        if handler_url:
            try:
                r = _url_shortcut(handler_url, title, link_dest, ctx.force)
                if r["type"] != "skipped_unchanged" and handler_id != _LINK_HANDLER:
                    r["type"] = "lti_tool"
                ctx.results.append(r)
            except ValueError as e:
                ctx.errors.append({"title": title, "error": str(e)})
        if body_html:
            await _handle_body(client, body_html, item_dir, ctx)

    # ------------------------------------------------------------------ #
    # 5. UNKNOWN / UNHANDLED  →  still try body links                     #
    # ------------------------------------------------------------------ #
    else:
        if body_html:
            await _handle_body(client, body_html, item_dir, ctx)

    # ------------------------------------------------------------------ #
    # 6. Always save body HTML as a standalone file (if rich content)     #
    # ------------------------------------------------------------------ #
    if body_html and handler_id not in _FILE_HANDLERS:
        _save_body_html(body_html, title, item_dir, ctx)


async def _download_attachments(
    client: BlackboardClient,
    course_id: str,
    content_id: str,
    dest: Path,
    title: str,
    attachments: list[dict],
    ctx: _Ctx,
) -> None:
    if not attachments:
        return

    dest.mkdir(parents=True, exist_ok=True)
    names = _assign_names([
        _fix_extension(a.get("fileName", "file"),
                       a.get("mimeType", "application/octet-stream"))
        for a in attachments
    ])

    async def _one(att: dict, name: str) -> None:
        att_id = att.get("id", "")
        mime   = att.get("mimeType", "application/octet-stream")
        size   = att.get("size")
        dest_file = dest / name

        if _up_to_date(dest_file, size, ctx.force):
            ctx.results.append({
                "saved": str(dest_file), "title": title, "filename": name,
                "size_bytes": dest_file.stat().st_size, "type": "skipped_unchanged",
            })
            return

        # Blackboard rarely includes downloadUrl in the attachments list, so
        # construct it from the known path pattern.
        dl_url = att.get("downloadUrl") or (
            f"{BB_HOST}/learn/api/public/v1/courses/{course_id}"
            f"/contents/{content_id}/attachments/{att_id}/download"
        )
        if dl_url.startswith("/"):
            dl_url = BB_HOST + dl_url

        try:
            async with ctx.sem:
                await client.download_file(dl_url, dest_path=dest_file)
            ctx.results.append({
                "saved": str(dest_file),
                "title": title,
                "filename": name,
                "mime": mime,
                "size_bytes": dest_file.stat().st_size,
                "type": "attachment",
            })
        except Exception as e:
            ctx.errors.append({"title": title, "filename": name, "error": str(e)})

    await asyncio.gather(*(_one(a, n) for a, n in zip(attachments, names)))


async def _handle_body(
    client: BlackboardClient,
    body_html: str,
    dest: Path,
    ctx: _Ctx,
) -> None:
    """Download files embedded in a content body (WebDav / BB-hosted links)."""
    links = _extract_body_links(body_html)
    if not links:
        return
    dest.mkdir(parents=True, exist_ok=True)
    names = _assign_names([f for _, f in links])

    async def _one(href: str, name: str) -> None:
        dest_file = dest / name
        if _up_to_date(dest_file, None, ctx.force):
            ctx.results.append({
                "saved": str(dest_file), "filename": name,
                "size_bytes": dest_file.stat().st_size, "type": "skipped_unchanged",
            })
            return
        try:
            async with ctx.sem:
                await client.download_file(href, dest_path=dest_file)
            ctx.results.append({
                "saved": str(dest_file),
                "filename": name,
                "size_bytes": dest_file.stat().st_size,
                "type": "body_embedded",
            })
        except Exception as e:
            ctx.errors.append({"href": href, "filename": name, "error": str(e)})

    await asyncio.gather(*(_one(h, n) for (h, _), n in zip(links, names)))


def _save_body_html(body_html: str, title: str, dest: Path, ctx: _Ctx) -> None:
    """Save the raw body HTML so the user can open it in a browser."""
    try:
        dest.mkdir(parents=True, exist_ok=True)
        html_file = dest / "_body.html"
        if html_file.exists() and not ctx.force and \
                html_file.read_text(encoding="utf-8", errors="replace") == body_html:
            ctx.results.append({"saved": str(html_file), "title": title,
                                "type": "skipped_unchanged"})
            return
        html_file.write_text(body_html, encoding="utf-8")
        ctx.results.append({
            "saved": str(html_file),
            "title": title,
            "type": "body_html",
        })
    except Exception:
        pass


def _summarize(base: Path, ctx: _Ctx) -> dict[str, Any]:
    downloaded = [r for r in ctx.results if r.get("type") != "skipped_unchanged"]
    skipped = [r for r in ctx.results if r.get("type") == "skipped_unchanged"]
    summary = {
        "save_dir": str(base),
        "files_saved": len(downloaded),
        "files_skipped_unchanged": len(skipped),
        "errors": len(ctx.errors),
        "total_bytes": sum(r.get("size_bytes", 0) for r in downloaded),
        "items": ctx.results,
        "error_details": ctx.errors,
    }
    if ctx.truncated:
        summary["truncated_at_max_depth"] = len(ctx.truncated)
        summary["truncated_note"] = (
            f"{len(ctx.truncated)} branches were not descended into because "
            f"max_depth was reached — raise max_depth to include them."
        )
    return summary


# ---------------------------------------------------------------------------
# Public tool functions
# ---------------------------------------------------------------------------

async def download_content(
    client: BlackboardClient,
    course_id: str,
    content_id: str,
    save_dir: str,
    max_depth: int = 6,
    force: bool = False,
    max_concurrency: int = DEFAULT_CONCURRENCY,
) -> dict[str, Any]:
    """
    Recursively download a content item and all its children to save_dir.

    save_dir is confined to the download root (BB_DOWNLOAD_ROOT). Files already
    present with a matching size are skipped unless force=True.
    """
    base = confine(save_dir)
    base.mkdir(parents=True, exist_ok=True)
    ctx = _Ctx(asyncio.Semaphore(max(1, max_concurrency)), force)

    await _walk(client, course_id, content_id, base, 0, max_depth, ctx)
    return _summarize(base, ctx)


async def download_course(
    client: BlackboardClient,
    course_id: str,
    save_dir: str,
    max_depth: int = 6,
    force: bool = False,
    max_concurrency: int = DEFAULT_CONCURRENCY,
) -> dict[str, Any]:
    """Download an entire course — every top-level item and everything under it."""
    base = confine(save_dir)
    base.mkdir(parents=True, exist_ok=True)
    ctx = _Ctx(asyncio.Semaphore(max(1, max_concurrency)), force)

    try:
        async with ctx.sem:
            roots = await client.get_contents(course_id)
    except Exception as e:
        raise RuntimeError(f"could not list course contents: {e}") from e

    await asyncio.gather(*(
        _walk(client, course_id, item["id"], base, 1, max_depth, ctx)
        for item in roots if item.get("id")
    ))

    summary = _summarize(base, ctx)
    summary["course_id"] = course_id
    summary["top_level_items"] = len(roots)
    return summary


async def list_files(
    client: BlackboardClient,
    course_id: str,
    content_id: Optional[str] = None,
    max_depth: int = 6,
    max_concurrency: int = DEFAULT_CONCURRENCY,
) -> list[dict[str, Any]]:
    """
    Dry-run tree walk — lists all downloadable files in a course or folder
    without downloading anything. Returns a flat list of file descriptors.
    """
    found: list[dict] = []
    sem = asyncio.Semaphore(max(1, max_concurrency))
    await _list_walk(client, course_id, content_id, [], 0, max_depth, found, sem)
    return found


async def _list_walk(
    client: BlackboardClient,
    course_id: str,
    content_id: Optional[str],
    path_parts: list[str],
    depth: int,
    max_depth: int,
    found: list[dict],
    sem: asyncio.Semaphore,
) -> None:
    if depth > max_depth:
        return

    # Top-level call: list root contents of the course
    if content_id is None:
        try:
            async with sem:
                children = await client.get_contents(course_id)
        except Exception:
            return
        await asyncio.gather(*(
            _list_walk(client, course_id, child["id"], path_parts,
                       depth + 1, max_depth, found, sem)
            for child in children if child.get("id")
        ))
        return

    try:
        async with sem:
            item = await client.get_content_item(course_id, content_id)
    except Exception:
        return

    handler_obj  = item.get("contentHandler") or {}
    handler_id   = handler_obj.get("id", "") if isinstance(handler_obj, dict) else ""
    handler_url  = handler_obj.get("url", "") if isinstance(handler_obj, dict) else ""
    title        = item.get("title", "untitled")
    has_children = item.get("hasChildren", False)
    body_html    = item.get("body", "") or ""

    current_path = path_parts + [safe_name(title)]

    if handler_id in _FILE_HANDLERS:
        try:
            async with sem:
                attachments = await client.get_attachments(course_id, content_id)
        except Exception:
            attachments = []
        for att in attachments:
            found.append({
                "path": "/".join(current_path + [att.get("fileName", "?")]),
                "content_id": content_id,
                "attachment_id": att.get("id"),
                "filename": att.get("fileName"),
                "mime": att.get("mimeType"),
                "size": att.get("size"),
                "type": "attachment",
            })

    elif handler_id in _FOLDER_HANDLERS or has_children:
        try:
            async with sem:
                children = await client.get_contents(course_id, content_id)
        except Exception:
            return
        await asyncio.gather(*(
            _list_walk(client, course_id, child["id"], current_path,
                       depth + 1, max_depth, found, sem)
            for child in children if child.get("id")
        ))

    elif handler_id in (_LINK_HANDLER, _BLTI_HANDLER) or \
            handler_id.startswith(_BLTI_PLACEMENT_PFX):
        if handler_url:
            found.append({
                "path": "/".join(current_path),
                "content_id": content_id,
                "url": handler_url,
                "type": "lti_tool" if "blti" in handler_id else "external_link",
            })

    # Body links are reported for EVERY handler, matching _walk — previously
    # only file handlers reported them, so the dry run under-listed what a real
    # download would fetch, and unknown handlers vanished from it entirely.
    for href, fname in _extract_body_links(body_html):
        found.append({
            "path": "/".join(current_path + [fname]),
            "content_id": content_id,
            "href": href,
            "filename": fname,
            "type": "body_embedded",
        })
