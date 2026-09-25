"""Body-text and link extraction from scraped HTML.

* Clean main text is extracted with ``trafilatura.extract`` and capped at
  ``max_text`` characters.
* ``<title>`` and ``<meta name="description">`` provide the metadata.
* Every ``<a href>`` is resolved against the page's ``final_url``, tagged
  with its anchor text and a ``same_domain`` flag, deduplicated, and capped
  at ``max_links`` items.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlparse

import trafilatura
from bs4 import BeautifulSoup
from bs4.element import Tag

from schemas.scrape import LinkItem

_DESCRIPTION_PATTERN = re.compile(r"^description$", re.IGNORECASE)
# Skip purely-technical targets that are not navigable content.
_SKIP_PREFIXES = ("#", "javascript:", "mailto:", "tel:", "data:", "ftp:")

_WHITESPACE = re.compile(r"[ \t\r\f\v]+")


@dataclass
class ExtractedContent:
    title: str | None
    meta_description: str | None
    text: str
    links: list[LinkItem]


def _attr(tag: Tag, name: str) -> str:
    """Read a tag attribute as a plain string.

    BeautifulSoup types an attribute as ``str | AttributeValueList | None``
    (multi-valued attributes such as ``class`` return a list). We only ever want
    the scalar text form, so normalise here instead of at every call site.
    """
    value = tag.get(name)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return " ".join(str(part) for part in value)


def extract_text(html: str, max_text: int) -> str:
    """Clean main-body text via trafilatura, capped at ``max_text`` chars."""
    text = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
        url=None,
    )
    if not text:
        return ""
    # Collapse runs of tabs/newlines into single spaces for a compact payload.
    text = _WHITESPACE.sub(" ", text.strip())
    return text[:max_text]


def extract_links(
    html: str | Tag,
    final_url: str,
    max_links: int,
    soup: BeautifulSoup | None = None,
) -> list[LinkItem]:
    """Resolve, deduplicate, and cap the anchors found in ``html``.

    ``soup`` lets a caller that already parsed the document pass the tree in,
    so a 10 MB page is not re-parsed just to walk its anchors.
    """
    if soup is None:
        tree = BeautifulSoup(html, "lxml") if isinstance(html, str) else html
    else:
        tree = soup
    final_host = urlparse(final_url).netloc.lower()

    out: list[LinkItem] = []
    seen: set[str] = set()
    for anchor in tree.find_all("a", href=True):
        href = _attr(anchor, "href").strip()
        if not href or href.lower().startswith(_SKIP_PREFIXES):
            continue
        abs_url = urldefrag(urljoin(final_url, href)).url
        if abs_url in seen:
            continue
        seen.add(abs_url)
        out.append(
            LinkItem(
                url=abs_url,
                anchor_text=anchor.get_text(strip=True),
                same_domain=urlparse(abs_url).netloc.lower() == final_host,
            )
        )
        if len(out) >= max_links:
            break
    return out


def extract_content(
    html: str,
    final_url: str,
    max_text: int,
    max_links: int,
) -> ExtractedContent:
    """Extract metadata, main text, and links from ``html`` in one pass."""
    soup = BeautifulSoup(html, "lxml")

    title = soup.title.get_text(strip=True) if soup.title else None
    meta = soup.find("meta", attrs={"name": _DESCRIPTION_PATTERN})
    meta_description = _attr(meta, "content").strip() if meta else None
    if meta is not None and meta_description in ("", None):
        meta = soup.find("meta", attrs={"property": _DESCRIPTION_PATTERN})
        meta_description = _attr(meta, "content").strip() if meta else None

    return ExtractedContent(
        title=title,
        meta_description=meta_description or None,
        text=extract_text(html, max_text),
        links=extract_links(soup, final_url, max_links, soup=soup),
    )
