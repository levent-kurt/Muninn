"""Sitemap detection and parsing.

A sitemap is identified by its URL shape (``sitemap*.xml``) or by an XML
content type returned by the server. Parsed ``<loc>`` entries are mapped to
:class:`~schemas.scrape.LinkItem` objects and returned immediately, bypassing
rendering and body-text extraction.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from schemas.scrape import LinkItem

XML_CONTENT_TYPES = {"text/xml", "application/xml", "application/x-xml"}


def looks_like_sitemap(url: str, content_type: str | None = None) -> bool:
    """Return True when ``url``/``content_type`` point at a sitemap document.

    Detection rules: a ``.xml`` URL extension, or a ``text/xml`` /
    ``application/xml`` content type returned by the server.
    """
    path = urlsplit(url).path.lower()
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct in XML_CONTENT_TYPES:
            return True
    return path.endswith(".xml")


def parse_sitemap(xml_text: str, max_links: int) -> list[LinkItem]:
    """Extract ``<loc>`` URLs from a sitemap (or sitemap index) document.

    Deduplicates URLs, discards fragments, and caps the output at
    ``max_links`` items.
    """
    soup = BeautifulSoup(xml_text, "xml")
    links: list[LinkItem] = []
    seen: set[str] = set()

    for loc in soup.find_all("loc"):
        raw = loc.get_text(strip=True)
        url = raw.split("#", 1)[0].strip()
        if not url or url in seen:
            continue
        seen.add(url)
        links.append(LinkItem(url=url, anchor_text="", same_domain=False))
        if len(links) >= max_links:
            break

    return links