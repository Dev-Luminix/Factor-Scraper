"""Crawl one investor website and POST the cleaned text to the monday code server.

    python crawl.py                                       # GitHub Actions: inputs from env vars
    python crawl.py --url https://example.com --dry-run   # local test: print the payload

Env vars: CRAWL_URL, ITEM_ID, BOARD_ID, CALLBACK_URL, CALLBACK_SECRET.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from urllib.parse import urlsplit

import requests
from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
from crawl4ai.deep_crawling import BFSDeepCrawlStrategy

import callback
from discover import (
    Site,
    canonical,
    load_robots,
    normalize_input_url,
    select_pages,
    sitemap_urls,
)
from extract import Page, build_pages, clean_html

USER_AGENT = (
    "Mozilla/5.0 (compatible; FactorScraper/1.0; +https://github.com/Dev-Luminix/Factor-Scraper) "
    "real-estate research crawler"
)
MAX_PAGES = 30
REQUEST_DELAY = 1.5  # seconds between page loads
PAGE_TIMEOUT_MS = 45_000
BFS_MAX_PAGES = 60
# "chrome" uses the Google Chrome preinstalled on GitHub's Ubuntu runners, so no browser
# download is needed. Locally the default is Playwright's Chromium (`crawl4ai-setup`).
BROWSER_CHANNEL = os.environ.get("BROWSER_CHANNEL", "chromium")
# Stop starting new pages after this long; the workflow itself times out at 30 minutes.
TIME_BUDGET = int(os.environ.get("CRAWL_TIME_BUDGET", 20 * 60))


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ---- fetching -----------------------------------------------------------------------------

def http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "he,en;q=0.8"})
    return session


def http_fetch(session: requests.Session, url: str) -> tuple[str, str] | None:
    """Plain HTTP fallback: (final URL, HTML) or None."""
    try:
        response = session.get(url, timeout=25, allow_redirects=True)
    except requests.RequestException:
        return None
    content_type = response.headers.get("Content-Type", "")
    if response.status_code != 200 or ("html" not in content_type and content_type):
        return None
    response.encoding = response.encoding if response.encoding and response.encoding.lower() != "iso-8859-1" else response.apparent_encoding
    return response.url, response.text


def resolve_home(session: requests.Session, url: str) -> tuple[str, str | None]:
    """Follow redirects to the site's real home URL. Falls back to http:// if https fails."""
    candidates = [url]
    if url.startswith("https://"):
        candidates.append("http://" + url[len("https://"):])
    for candidate in candidates:
        fetched = http_fetch(session, candidate)
        if fetched:
            return canonical(fetched[0]), fetched[1]
    # The browser may still get through (some sites block plain HTTP clients).
    return url, None


def run_config(**extra) -> CrawlerRunConfig:
    return CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=PAGE_TIMEOUT_MS,
        wait_until="domcontentloaded",
        delay_before_return_html=1.5,  # let client-side rendering finish
        remove_overlay_elements=True,
        remove_consent_popups=True,
        check_robots_txt=True,
        user_agent=USER_AGENT,
        exclude_external_links=True,
        verbose=False,
        **extra,
    )


async def crawl_site(start_url: str, max_pages: int) -> tuple[str, list[Page]]:
    """Discover, select and fetch pages. Returns the site's home URL and the cleaned pages."""
    deadline = time.monotonic() + TIME_BUDGET
    session = http_session()
    home, home_html = resolve_home(session, start_url)
    site = Site(home)
    robots, listed_sitemaps = load_robots(session, site)
    allowed = lambda u: robots.can_fetch(USER_AGENT, u)  # noqa: E731

    candidates = sitemap_urls(session, site, listed_sitemaps)
    log(f"Home: {home} | sitemap pages: {len(candidates)}")

    html_by_url: dict[str, tuple[str, str]] = {}  # canonical URL -> (URL, HTML)
    browser = BrowserConfig(headless=True, user_agent=USER_AGENT, verbose=False, chrome_channel=BROWSER_CHANNEL)
    async with AsyncWebCrawler(config=browser) as crawler:
        if len(candidates) < 3:
            # No usable sitemap: breadth-first crawl of the site itself (same domain, depth 2).
            strategy = BFSDeepCrawlStrategy(max_depth=2, include_external=False, max_pages=BFS_MAX_PAGES)
            results = await crawler.arun(home, config=run_config(deep_crawl_strategy=strategy, semaphore_count=2, mean_delay=REQUEST_DELAY))
            for result in results if isinstance(results, list) else [results]:
                final = result.redirected_url or result.url
                if result.success and result.html and site.owns(final):
                    html_by_url[canonical(final)] = (canonical(final), result.html)
                    candidates.append(final)
                for link in (result.links or {}).get("internal", []):
                    if link.get("href"):
                        candidates.append(link["href"])
            log(f"Deep crawl: {len(html_by_url)} pages fetched, {len(candidates)} links found")

        selected = [u for u in select_pages(site, candidates, max_pages) if allowed(u)]
        log(f"Selected {len(selected)} pages")

        for index, url in enumerate(selected):
            if canonical(url) in html_by_url:
                continue
            if time.monotonic() > deadline:
                log("Time budget reached; sending what was collected")
                break
            if index:
                await asyncio.sleep(REQUEST_DELAY)
            page = None
            try:
                result = await crawler.arun(url, config=run_config())
                final = result.redirected_url or result.url
                ok_status = result.status_code is None or result.status_code < 400
                if result.success and result.html and ok_status and site.owns(final):
                    page = (canonical(final), result.html)
            except Exception as err:  # the browser can fail on odd pages; fall back below
                log(f"Browser error on a page: {type(err).__name__}")
            if page is None:
                fetched = http_fetch(session, url)
                if fetched and site.owns(fetched[0]):
                    page = (canonical(fetched[0]), fetched[1])
            if page is None and url == home and home_html:
                page = (home, home_html)
            if page:
                html_by_url.setdefault(page[0], page)

    selected_keys = {canonical(u) for u in selected}
    pages = []
    for key, (url, html) in html_by_url.items():
        if key not in selected_keys and len(pages) >= max_pages:
            continue
        title, lines = clean_html(html)
        pages.append(Page(url=url, title=title, lines=lines))
    return home, pages[: max_pages * 2]


# ---- main ---------------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("CRAWL_URL"))
    parser.add_argument("--item-id", default=os.environ.get("ITEM_ID", "0"))
    parser.add_argument("--board-id", default=os.environ.get("BOARD_ID", "0"))
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--dry-run", action="store_true", help="print the payload instead of posting it")
    return parser.parse_args()


def deliver(payload: dict, dry_run: bool) -> int:
    if dry_run:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        total = sum(len(p["text"]) for p in payload["pages"])
        log(f"[dry run] status={payload['status']} pages={len(payload['pages'])} chars={total} hash={payload['content_hash'][:12]}…")
        return 0
    try:
        status = callback.post(os.environ.get("CALLBACK_URL", ""), os.environ.get("CALLBACK_SECRET", ""), payload)
    except callback.CallbackError as err:
        log(f"Callback failed: {err}")
        return 1
    callback.mark_sent()
    log(f"Callback delivered (HTTP {status}): status={payload['status']}, {len(payload['pages'])} pages")
    return 0


def main() -> int:
    args = parse_args()
    ids = {"item_id": args.item_id, "board_id": args.board_id}
    raw_url = args.url or ""

    try:
        start_url = normalize_input_url(raw_url)
    except ValueError as err:
        return deliver(callback.build_payload(**ids, url=raw_url, status="error", error=str(err)), args.dry_run)

    try:
        home, pages = asyncio.run(crawl_site(start_url, args.max_pages))
    except Exception as err:
        message = f"Crawl failed: {type(err).__name__}: {err}"[:500]
        return deliver(callback.build_payload(**ids, url=start_url, status="error", error=message), args.dry_run)

    if not pages:
        host = urlsplit(start_url).hostname
        error = f"Could not load any page from {host} (blocked, offline, or disallowed by robots.txt)"
        return deliver(callback.build_payload(**ids, url=start_url, status="error", error=error), args.dry_run)

    # Even very little text is a success; the server decides whether it's enough.
    payload = callback.build_payload(**ids, url=home, status="success", pages=build_pages(pages))
    return deliver(payload, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
