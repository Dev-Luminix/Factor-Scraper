"""Find and rank the pages of one website.

URLs are normalized, kept on the site's own domain (www and non-www count as the same site),
and ranked by their URL-decoded path so Hebrew slugs like /פרויקטים/ are understood.
"""

from __future__ import annotations

import gzip
import html
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests

# ---- URLs --------------------------------------------------------------------------------

SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tif", ".tiff",
    ".zip", ".rar", ".7z", ".gz", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv",
    ".mp4", ".mp3", ".mov", ".avi", ".webm", ".wav", ".css", ".js", ".json", ".xml", ".txt",
    ".woff", ".woff2", ".ttf", ".eot", ".dwg", ".exe", ".apk",
)
SKIP_PATHS = re.compile(
    r"/(wp-admin|wp-json|wp-login|wp-content|xmlrpc|feed|cart|checkout|my-account|login|logout|"
    r"register|cdn-cgi|search)(/|$)",
    re.IGNORECASE,
)
TRACKING_PARAMS = re.compile(r"^(utm_\w+|fbclid|gclid|mc_\w+|_ga|ref|replytocom|share)$", re.IGNORECASE)


def normalize_input_url(raw: str) -> str:
    """Turn user input into an absolute http(s) URL. Raises ValueError if it isn't one."""
    value = (raw or "").strip()
    if not value:
        raise ValueError("No URL given")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", value, re.IGNORECASE):
        value = "https://" + value
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or "." not in host or host.startswith(".") or host.endswith("."):
        raise ValueError(f"Not a website URL: {raw!r}")
    return canonical(value)


def canonical(url: str) -> str:
    """Comparable form: lower-case host, no fragment, no tracking params, no trailing slash."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)])
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


def site_key(host: str) -> str:
    host = host.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


@dataclass(frozen=True)
class Site:
    """The website being crawled: its final URL after redirects and its domain."""

    home: str

    @property
    def key(self) -> str:
        return site_key(urlsplit(self.home).hostname or "")

    def owns(self, url: str) -> bool:
        """Same domain (www or not), http(s) only."""
        parts = urlsplit(url)
        return parts.scheme in ("http", "https") and site_key(parts.hostname or "") == self.key

    def is_crawlable(self, url: str) -> bool:
        if not self.owns(url):
            return False
        path = urlsplit(url).path.lower()
        return not path.endswith(SKIP_EXTENSIONS) and not SKIP_PATHS.search(path)


# ---- ranking -----------------------------------------------------------------------------

def _norm_text(value: str) -> str:
    """Lower case, no quote marks (נדל"ן → נדלן), separators as spaces."""
    value = unquote(value).lower()
    value = re.sub(r"[\"'`׳״‘’“”]", "", value)
    return re.sub(r"[-_/+.%]+", " ", value).strip()


PROJECT_WORDS = [
    "project", "projects", "residential", "commercial", "properties", "property", "portfolio",
    "development", "developments", "real estate", "realestate", "building", "buildings",
    "פרויקט", "פרויקטים", "פרוייקטים", "נכסים", "נכס", "מגורים", 'נדל"ן', "מסחרי", "משרדים",
    "התחדשות עירונית", "תמא", "פינוי בינוי",
]
ABOUT_WORDS = ["about", "about us", "company", "who we are", "אודות", "החברה", "מי אנחנו", "אודותינו"]
CONTACT_WORDS = ["contact", "contact us", "צור קשר", "צרו קשר", "יצירת קשר"]
LOW_VALUE_WORDS = [
    "blog", "news", "article", "articles", "tag", "category", "author", "privacy", "terms",
    "cookies", "accessibility", "careers", "jobs", "event", "events",
    "בלוג", "חדשות", "כתבות", "מדיניות", "פרטיות", "תקנון", "נגישות", "דרושים", "קריירה",
]
_PROJECT = [_norm_text(w) for w in PROJECT_WORDS]
_ABOUT = [_norm_text(w) for w in ABOUT_WORDS]
_CONTACT = [_norm_text(w) for w in CONTACT_WORDS]
_LOW = [_norm_text(w) for w in LOW_VALUE_WORDS]
_PAGINATION = re.compile(r"/page/\d+/?$", re.IGNORECASE)


def _has(path_text: str, words: list[str]) -> bool:
    padded = f" {path_text} "
    return any(f" {w} " in padded or (len(w) > 4 and w in path_text) for w in words)


def path_text(url: str) -> str:
    parts = urlsplit(url)
    return _norm_text(parts.path + (" " + parts.query if parts.query else ""))


def is_project_page(url: str) -> bool:
    return _has(path_text(url), _PROJECT)


def is_about_page(url: str) -> bool:
    return _has(path_text(url), _ABOUT)


def is_home(url: str) -> bool:
    parts = urlsplit(url)
    return parts.path in ("", "/") and not parts.query


def score(url: str) -> float:
    """Higher is more useful for finding where the company operates."""
    text = path_text(url)
    depth = len([s for s in urlsplit(url).path.split("/") if s])
    value = 0.0
    if _has(text, _PROJECT):
        value += 10
    if _has(text, _ABOUT):
        value += 6
    if _has(text, _CONTACT):
        value += 5
    if _has(text, _LOW) or _PAGINATION.search(urlsplit(url).path):
        value -= 6
    return value - 0.5 * depth


def select_pages(site: Site, candidates: list[str], limit: int) -> list[str]:
    """Homepage first, then relevant pages by score. Low-value pages only fill leftover slots."""
    unique: dict[str, str] = {}
    for url in [site.home, *candidates]:
        if site.is_crawlable(url):
            unique.setdefault(canonical(url), url)
    home = canonical(site.home)
    rest = [u for u in unique if u != home]
    rest.sort(key=lambda u: (-score(u), len(urlsplit(u).path), u))
    useful = [u for u in rest if score(u) > -3]
    low = [u for u in rest if score(u) <= -3]
    return [home, *useful, *low][:limit]


# ---- sitemaps and robots.txt ----------------------------------------------------------------

SITEMAP_PATHS = ["/sitemap_index.xml", "/sitemap.xml", "/wp-sitemap.xml"]
MAX_SITEMAP_FILES = 25
MAX_SITEMAP_URLS = 5000
_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", re.IGNORECASE | re.DOTALL)
# Child sitemaps worth reading first (pages and projects before posts, tags and products).
_SITEMAP_PRIORITY = re.compile(r"page|project|property|portfolio|פרויקט|נכס", re.IGNORECASE)
_SITEMAP_LOW = re.compile(r"post_tag|tag|category|author|product|attachment|media|image", re.IGNORECASE)


def fetch_text(session: requests.Session, url: str, timeout: float = 20) -> str | None:
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True)
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    content = response.content
    if content[:2] == b"\x1f\x8b":
        try:
            content = gzip.decompress(content)
        except OSError:
            return None
    return content.decode(response.encoding or "utf-8", "replace")


def load_robots(session: requests.Session, site: Site) -> tuple[RobotFileParser, list[str]]:
    """robots.txt rules and the sitemaps it lists. A missing robots.txt allows everything."""
    parser = RobotFileParser()
    origin = f"{urlsplit(site.home).scheme}://{urlsplit(site.home).netloc}"
    text = fetch_text(session, origin + "/robots.txt")
    lines = (text or "").splitlines()
    parser.parse(lines)
    sitemaps = [l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("sitemap:")]
    return parser, sitemaps


def sitemap_urls(session: requests.Session, site: Site, listed: list[str]) -> list[str]:
    """Page URLs from the site's sitemaps (robots.txt entries first, then the usual paths)."""
    origin = f"{urlsplit(site.home).scheme}://{urlsplit(site.home).netloc}"
    queue = [*listed, *(origin + p for p in SITEMAP_PATHS)]
    seen_files: set[str] = set()
    pages: list[str] = []

    while queue and len(seen_files) < MAX_SITEMAP_FILES and len(pages) < MAX_SITEMAP_URLS:
        sitemap = queue.pop(0)
        if sitemap in seen_files or not site.owns(sitemap):
            continue
        seen_files.add(sitemap)
        text = fetch_text(session, sitemap)
        if not text or "<loc" not in text.lower():
            continue
        locs = [html.unescape(m).strip() for m in _LOC.findall(text)]
        if "<sitemapindex" in text.lower():
            children = [l for l in locs if l not in seen_files]
            children.sort(key=lambda l: (0 if _SITEMAP_PRIORITY.search(unquote(l)) else 2 if _SITEMAP_LOW.search(l) else 1))
            queue = children + queue
            continue
        pages.extend(l for l in locs if site.is_crawlable(l))
    return pages[:MAX_SITEMAP_URLS]
