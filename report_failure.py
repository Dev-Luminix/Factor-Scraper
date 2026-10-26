"""Send an error callback when the crawl workflow fails, so the server never waits forever.

Runs as the workflow's `if: failure()` step. Standard library only. Does nothing if crawl.py
already delivered a result.
"""

from __future__ import annotations

import os
import sys

from callback import SENT_MARKER, CallbackError, build_payload, post


def main() -> int:
    if SENT_MARKER.exists():
        print("A result was already delivered; not sending an error.")
        return 0

    run_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        run_url = f" Run: {server}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ['GITHUB_RUN_ID']}"

    payload = build_payload(
        item_id=os.environ.get("ITEM_ID", ""),
        board_id=os.environ.get("BOARD_ID", ""),
        url=os.environ.get("CRAWL_URL", ""),
        status="error",
        error=f"Crawl workflow failed before reporting a result.{run_url}",
    )
    try:
        status = post(os.environ.get("CALLBACK_URL", ""), os.environ.get("CALLBACK_SECRET", ""), payload)
    except CallbackError as err:
        print(f"Could not send the error callback: {err}", file=sys.stderr)
        return 1
    print(f"Error callback sent (HTTP {status}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
