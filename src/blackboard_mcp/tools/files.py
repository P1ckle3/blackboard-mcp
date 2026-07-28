"""Tool: download_file — fetch a Blackboard attachment."""

from __future__ import annotations

import base64
from typing import Any, Optional

from ..api import BB_HOST, BlackboardClient
from ..paths import confine, safe_name, unique_path

# Inline base64 costs ~1.33x the file size as a JSON string on top of the bytes
# themselves; anything larger blows the MCP message limit before it OOMs.
MAX_INLINE_BYTES = 8 * 1024 * 1024


async def download_file(
    client: BlackboardClient,
    course_id: str,
    content_id: str,
    attachment_id: Optional[str] = None,
    save_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Download a file attachment from a content item.

    If save_path is given, writes the file there (confined to the download
    root) and returns the path. Otherwise returns base64-encoded content,
    provided the file is under MAX_INLINE_BYTES.
    """
    attachments = await client.get_attachments(course_id, content_id)
    if not attachments:
        raise ValueError("No attachments found on this content item.")

    # Pick the specified attachment or the first one
    if attachment_id:
        target = next((a for a in attachments if a.get("id") == attachment_id), None)
        if not target:
            raise ValueError(f"Attachment {attachment_id} not found.")
    else:
        target = attachments[0]

    # Blackboard often omits downloadUrl — build it from the attachment ID
    download_url = target.get("downloadUrl") or (
        f"{BB_HOST}/learn/api/public/v1/courses/{course_id}"
        f"/contents/{content_id}/attachments/{target['id']}/download"
    )
    if download_url.startswith("/"):
        download_url = BB_HOST + download_url

    filename = target.get("fileName", "file")
    mime_type = target.get("mimeType", "application/octet-stream")

    if save_path:
        dest = confine(save_path)
        dest = unique_path(dest.parent / safe_name(dest.name))
        await client.download_file(download_url, dest_path=dest)
        return {
            "saved_to": str(dest),
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": dest.stat().st_size,
        }

    size_hint = target.get("size") or 0
    if size_hint > MAX_INLINE_BYTES:
        raise ValueError(
            f"{filename} is {size_hint / 1e6:.1f}MB — too large to return inline. "
            f"Pass save_path to download it to disk instead."
        )

    data = await client.download_file(download_url)
    if len(data) > MAX_INLINE_BYTES:
        raise ValueError(
            f"{filename} is {len(data) / 1e6:.1f}MB — too large to return inline. "
            f"Pass save_path to download it to disk instead."
        )

    return {
        "filename": filename,
        "mime_type": mime_type,
        "size_bytes": len(data),
        "content_base64": base64.b64encode(data).decode("ascii"),
    }
