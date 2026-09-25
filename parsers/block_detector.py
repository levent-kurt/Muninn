"""Block & fallback detection engine.

Flags a page as blocked/suspect when:

* the upstream status is ``403`` / ``429`` / ``503``;
* the HTML contains Cloudflare / challenge markers such as
  ``Just a moment...``, ``cf-chl``, ``g-recaptcha``, ``captcha-delivery``;
* the extracted text is tiny (< ``text_min_char_threshold`` chars) while the
  raw HTML body is large (> ``text_anomaly_html_bytes`` - the classic
  "page shell but no content" signature);
* clients explicitly request ``render=1`` (fast-path result is never trusted).

The detector is the single place these rules live; the orchestrator composes
``quick_block`` (cheap checks) with ``thin_text_block`` (requires the
already-extracted text so it is not computed twice).
"""

from __future__ import annotations

from dataclasses import dataclass

CHALLENGE_MARKERS: tuple[str, ...] = (
    "just a moment...",
    "cf-chl",
    "cf_chl",
    "cf-chl-solving",
    "g-recaptcha",
    "recaptcha",
    "captcha-delivery",
    "checking your browser",
    "checking if the site connection is secure",
)

# Upstream status codes that always imply a bot-block / rate limit.
BLOCKED_STATUSES: frozenset[int] = frozenset({403, 429, 503})

REASON_HTTP = "http-block"
REASON_CHALLENGE = "challenge"
REASON_THIN_TEXT = "thin-text"
REASON_EMPTY = "empty-body"
REASON_RENDER_REQUESTED = "render-requested"


@dataclass(frozen=True)
class BlockInfo:
    blocked: bool
    reason: str | None = None


def quick_block(
    html: str | None,
    status: int | None,
    render_requested: bool = False,
) -> BlockInfo:
    """Cheap checks: status code, challenge markers, empty body, render flag."""
    if status in BLOCKED_STATUSES:
        return BlockInfo(True, REASON_HTTP)
    if html is None or not html.strip():
        return BlockInfo(True, REASON_EMPTY)
    lowered = html.lower()
    for marker in CHALLENGE_MARKERS:
        if marker in lowered:
            return BlockInfo(True, REASON_CHALLENGE)
    if render_requested:
        return BlockInfo(True, REASON_RENDER_REQUESTED)
    return BlockInfo(False)


def thin_text_block(
    html_size_bytes: int,
    text_len: int,
    html_threshold: int,
    text_threshold: int,
) -> BlockInfo:
    """Flag pages whose shell is large but whose real content is tiny."""
    if html_size_bytes > html_threshold and text_len < text_threshold:
        return BlockInfo(True, REASON_THIN_TEXT)
    return BlockInfo(False)
