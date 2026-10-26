"""Callback to the monday code server: build, sign and POST the crawl result.

Standard library only, so report_failure.py works even when `pip install` failed.

Contract (must match the server exactly):
    POST CALLBACK_URL
    Content-Type: application/json
    X-Signature: "sha256=" + hex HMAC-SHA256 of the exact body bytes, key = CALLBACK_SECRET
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

USER_AGENT = "FactorScraper/1.0 (+https://github.com/Dev-Luminix/Factor-Scraper)"

# Written after a result is delivered, so report_failure.py never overwrites it with an error.
SENT_MARKER = Path(".callback-sent")


class CallbackError(Exception):
    """The server rejected the callback or could not be reached after all retries."""


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(pages: list[dict[str, str]]) -> str:
    """sha256 of the combined page text in URL order, so an unchanged site gives the same hash."""
    ordered = sorted(pages, key=lambda p: p["url"])
    return sha256_hex("\n\n".join(p["text"] for p in ordered))


def build_payload(
    *,
    item_id: str,
    board_id: str,
    url: str,
    status: str,
    error: str | None = None,
    pages: list[dict[str, str]] | None = None,
    crawled_at: str | None = None,
) -> dict[str, Any]:
    if status not in ("success", "error"):
        raise ValueError(f"status must be 'success' or 'error', not {status!r}")
    pages = [{"url": p["url"], "title": p.get("title", ""), "text": p.get("text", "")} for p in (pages or [])]
    return {
        "item_id": str(item_id),
        "board_id": str(board_id),
        "url": url,
        "status": status,
        "error": error,
        "content_hash": content_hash(pages),
        "pages": pages,
        "crawled_at": crawled_at or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def serialize(payload: dict[str, Any]) -> bytes:
    """Serialize once. These exact bytes are signed and sent."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def post(
    callback_url: str,
    secret: str,
    payload: dict[str, Any],
    *,
    retries: int = 3,
    timeout: float = 60,
    sleep=time.sleep,
) -> int:
    """POST the payload. Retries up to `retries` times (2s, 4s, 8s) on network errors and 5xx.

    A 4xx answer is not retried: the request itself is wrong (bad signature or payload).
    Returns the HTTP status; raises CallbackError on failure.
    """
    if not callback_url or not secret:
        raise CallbackError("CALLBACK_URL and CALLBACK_SECRET must be set")
    body = serialize(payload)
    headers = {
        "Content-Type": "application/json",
        "X-Signature": sign(body, secret),
        "User-Agent": USER_AGENT,
    }

    last_error = "unknown error"
    for attempt in range(retries + 1):
        if attempt:
            sleep(2 ** attempt)
        request = urllib.request.Request(callback_url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status
        except urllib.error.HTTPError as err:
            detail = err.read(300).decode("utf-8", "replace")
            last_error = f"HTTP {err.code}: {detail}"
            if err.code < 500:
                raise CallbackError(f"Callback rejected ({last_error})") from err
        except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
            last_error = f"network error: {getattr(err, 'reason', err)}"
    raise CallbackError(f"Callback failed after {retries + 1} attempts ({last_error})")


def mark_sent() -> None:
    SENT_MARKER.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
