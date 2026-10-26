"""Turn page HTML into clean, relevant text.

1. clean_html: drop navigation, header, footer, cookie banners, scripts and forms; keep the
   title and the main text as lines ("paragraphs").
2. remove_boilerplate: drop lines that repeat on most pages (menus, footers, slogans).
3. build_pages: keep paragraphs that mention places, addresses, projects or units, plus the
   start of the about page, and cap the total size, preferring project pages.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, NavigableString, Tag

import places
from discover import is_about_page, is_home, is_project_page, score

MAX_TOTAL_CHARS = 60_000
ABOUT_HEAD_CHARS = 1_500
# Below this much relevant text, send the cleaned text unfiltered rather than almost nothing.
MIN_RELEVANT_CHARS = 1_500

# ---- cleaning ----------------------------------------------------------------------------

REMOVE_TAGS = [
    "script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed",
    "nav", "form", "button", "select", "input", "textarea", "label", "dialog",
]
# Site-wide header/footer are removed; a header inside <article>/<main> (e.g. a project title) stays.
PAGE_CHROME_TAGS = ["header", "footer"]
REMOVE_ROLES = {"navigation", "banner", "contentinfo", "dialog", "alertdialog", "search", "menu", "menubar"}
# id/class names of menus, banners, popups and widgets that aren't page content.
BOILERPLATE_NAMES = re.compile(
    r"cookie|consent|gdpr|popup|modal|newsletter|subscribe|breadcrumb|navbar|nav-menu|main-menu|"
    r"mobile-menu|menu-item|offcanvas|off-canvas|sidebar|widget_|share|social|whatsapp|skip-link|"
    r"screen-reader|sr-only|visually-hidden|accessibility|nagish|a11y|site-header|site-footer|"
    r"location-header|location-footer|elementor-location-popup|back-to-top|scroll-top",
    re.IGNORECASE,
)
BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "aside", "li", "ul", "ol", "dl", "dt", "dd", "table",
    "tr", "td", "th", "thead", "tbody", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
    "figure", "figcaption", "address", "pre", "hr", "br",
}
_SPACES = re.compile(r"[ \t ​‎‏‪-‮]+")
_HAS_WORD = re.compile(r"[^\W_]", re.UNICODE)


def _attr_names(tag: Tag) -> str:
    classes = tag.get("class") or []
    if isinstance(classes, str):
        classes = [classes]
    return " ".join([tag.get("id") or "", *classes])


def clean_html(html: str) -> tuple[str, list[str]]:
    """Title and main-text lines of one page."""
    soup = BeautifulSoup(html or "", "lxml")
    title = ""
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        title = og["content"]
    elif soup.title and soup.title.string:
        title = soup.title.string
    elif soup.h1:
        title = soup.h1.get_text(" ")
    title = _SPACES.sub(" ", title).strip()[:300]

    body = soup.body or soup
    for tag in body.find_all(REMOVE_TAGS):
        tag.decompose()
    for tag in body.find_all(PAGE_CHROME_TAGS):
        if not tag.decomposed and tag.find_parent(["article", "main"]) is None:
            tag.decompose()
    total = len(body.get_text(" ", strip=True)) or 1
    for tag in body.find_all(True):
        if tag.decomposed or tag.name in ("body", "html", "main"):
            continue
        role = (tag.get("role") or "").lower()
        # aria-hidden marks duplicated menus/sliders; the `hidden` attribute is left alone
        # because tabs and accordions on project pages often start hidden.
        hidden = tag.get("aria-hidden") == "true"
        if role in REMOVE_ROLES or hidden or BOILERPLATE_NAMES.search(_attr_names(tag)):
            # Never let a broad class match remove most of the page.
            if len(tag.get_text(" ", strip=True)) < 0.6 * total:
                tag.decompose()

    # Use <main> when it holds real content; otherwise the whole body.
    main = body.find("main")
    root = main if main and len(main.get_text(" ", strip=True)) > 200 else body

    # Put line breaks around block elements so inline text (e.g. "<b>120</b> units") stays joined.
    for tag in root.find_all(BLOCK_TAGS):
        tag.insert_before(NavigableString("\n"))
        tag.insert_after(NavigableString("\n"))
    lines: list[str] = []
    seen: set[str] = set()
    for raw in root.get_text("").split("\n"):
        line = _SPACES.sub(" ", raw).strip()
        if len(line) < 2 or not _HAS_WORD.search(line) or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return title, lines


# ---- boilerplate across pages ---------------------------------------------------------------

@dataclass
class Page:
    url: str
    title: str
    lines: list[str] = field(default_factory=list)


def remove_boilerplate(pages: list[Page]) -> list[Page]:
    """Drop lines that appear on at least half of the pages (min. 3 pages)."""
    if len(pages) < 3:
        return pages
    counts: dict[str, int] = {}
    for page in pages:
        for line in set(page.lines):
            counts[line] = counts.get(line, 0) + 1
    threshold = max(3, math.ceil(len(pages) / 2))
    common = {line for line, n in counts.items() if n >= threshold}
    return [Page(p.url, p.title, [l for l in p.lines if l not in common]) for p in pages]


# ---- relevance ----------------------------------------------------------------------------

UNITS = re.compile(
    r'יח["״\']?ד|יחידות דיור|דירות|דירה|קומות|קומה|מ["״\']?ר|דונם|חדרים|'
    r"\bunits?\b|\bapartments?\b|\bfloors?\b|\bstoreys?\b|\bsq\.? ?f(ee)?t\b|\bsqm\b|m²|"
    r"square (feet|met(er|re)s)|\bdunams?\b|\bacres?\b",
    re.IGNORECASE,
)
PROJECTS = re.compile(
    r'פרויקט|פרוייקט|שכונ|מגדל|בניין|בנין|מתחם|תמ["״]?א|פינוי|בינוי|התחדשות|היתר|תב["״]?ע|בנייה|בניה|'
    r"יזמ|ייזום|מגורים|מסחר|משרדים|תעשי|לוגיסטי|קניון|מלון|"
    r"\bprojects?\b|\btowers?\b|\bbuildings?\b|\bdevelopments?\b|\bresidential\b|\bcommercial\b|"
    r"\boffices?\b|\bindustrial\b|\blogistics\b|\bmall\b|\bhotels?\b|\bneighbou?rhoods?\b|"
    r"\bcomplex\b|\bcampus\b|\brenewal\b",
    re.IGNORECASE,
)
ADDRESSES = re.compile(
    r"רחוב|רח['׳]|שד['׳]|שדרות|שדרת|כתובת|מיקום|ממוקמ|בעיר|"
    r"\bstreet\b|\bst\.|\broad\b|\brd\.|\bavenue\b|\bave\.|\bboulevard\b|\bblvd\b|\baddress\b|"
    r"\blocated\b|\blocation\b",
    re.IGNORECASE,
)
PLACES = re.compile(
    "|".join(
        [re.escape(p) for p in places.HEBREW]
        + [r"\b" + re.escape(p) + r"\b" for p in places.ENGLISH]
    ),
    re.IGNORECASE,
)


def is_relevant(line: str) -> bool:
    return bool(UNITS.search(line) or PROJECTS.search(line) or ADDRESSES.search(line) or PLACES.search(line))


def _page_order(url: str) -> tuple:
    """Budget order: project pages, homepage, about page, then the rest by score."""
    group = 0 if is_project_page(url) else 1 if is_home(url) else 2 if is_about_page(url) else 3
    return (group, -score(url), url)


def _relevant_text(page: Page) -> str:
    kept = [l for l in page.lines if is_relevant(l)]
    if is_about_page(page.url):
        full = "\n".join(page.lines)
        head = full[:ABOUT_HEAD_CHARS]
        kept = [head] + [l for l in kept if l not in head]
    return "\n".join(kept).strip()


def _cap(texts: list[tuple[Page, str]], limit: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    budget = limit
    for page, text in texts:
        if budget <= 0:
            break
        if not text:
            continue
        text = text[:budget]
        budget -= len(text) + 2
        out.append({"url": page.url, "title": page.title, "text": text})
    return out


def build_pages(pages: list[Page], limit: int = MAX_TOTAL_CHARS) -> list[dict[str, str]]:
    """Payload pages: relevant text, project pages first, at most `limit` characters in total."""
    pages = sorted(remove_boilerplate(pages), key=lambda p: _page_order(p.url))
    relevant = [(p, _relevant_text(p)) for p in pages]
    if sum(len(t) for _, t in relevant) >= MIN_RELEVANT_CHARS:
        return _cap(relevant, limit)
    # Too little matched the filters (small or unusual site): send the cleaned text instead.
    return _cap([(p, "\n".join(p.lines)) for p in pages], limit)
