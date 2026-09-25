"""Scrape pipeline orchestrator.

Pipeline for one ``GET /scrape`` request::

    target guard          -> SSRF + robots.txt policy (ops/netguard, ops/robots)
    cache hit?            -> return cached response immediately
    politeness slot       -> host-lock + mandatory gap for the whole request
    fast-path fetch       -> plain HTTP, redirects tracked
    sitemap?              -> parse <loc> entries, return (no render/text pass)
    block_detector        -> status / challenge markers / render=1 / thin-text
    clean + render=0      -> extract text+links and return
    blocked or render=1   -> escalate to the Stealth Browser Pool, re-evaluate
    save to cache         -> TTL-cached by URL for identical follow-ups
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from app.config import Settings
from fetchers.fast_path import FastPathResult
from ops.netguard import validate_target_url
from ops.robots import RobotsGate
from parsers import block_detector
from parsers.content import extract_content
from parsers.sitemap import looks_like_sitemap, parse_sitemap
from schemas.scrape import LinkItem, ScrapeResponse

if TYPE_CHECKING:
    from browser_pool.manager import BrowserPoolManager, RenderOutcome
    from fetchers.fast_path import FastPathFetcher
    from ops.cache import ScrapeCache
    from ops.politeness import HostPoliteness


class ScrapeService:
    """Coordinates the fast-path / browser-pool scrape legs for one URL."""

    def __init__(
        self,
        settings: Settings,
        *,
        fetcher: FastPathFetcher,
        politeness: HostPoliteness,
        cache: ScrapeCache,
        browser_pool: BrowserPoolManager,
        robots: RobotsGate | None = None,
        validator: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._settings = settings
        self._fetcher = fetcher
        self._politeness = politeness
        self._cache = cache
        self._pool = browser_pool
        # Both guards default to the real, settings-driven implementations.
        # Tests inject their own to stay offline; neither has an off switch here,
        # because a guard that must be re-enabled in two places will be forgotten.
        self._robots = (
            robots
            if robots is not None
            else (
                RobotsGate(settings.user_agent, settings.scrape_fast_path_timeout)
                if settings.scrape_respect_robots
                else None
            )
        )
        self._validator = validator or self._default_validator

    async def _default_validator(self, url: str) -> str:
        return await validate_target_url(
            url,
            allow_private=self._settings.scrape_allow_private_targets,
            allowed_hosts=self._settings.scrape_allowed_hosts,
        )

    # -- public ---------------------------------------------------------------

    async def scrape(
        self,
        url: str,
        render: int = 0,
        max_text: int | None = None,
    ) -> ScrapeResponse:
        # Honour the caller's size request, bounded by the configured ceiling.
        # (Clamping to default_max_text instead would make the endpoint's own
        # max_text upper bound meaningless.)
        max_text = min(
            max(1, max_text or self._settings.default_max_text),
            self._settings.max_text_cap,
        )
        max_links = self._settings.max_links_cap
        render_flag = 1 if render else 0

        # Guard first, cache second: the cache is keyed on the requested URL, so
        # a target that has since become disallowed (robots changed, host turned
        # internal) would otherwise keep being served straight from cache.
        url = await self._validator(url)

        hit = await self._cache.get(url, render_flag)
        if hit is not None:
            return hit

        if self._robots is not None:
            await self._robots.check(url)

        async with self._politeness.slot(url):
            fast = await self._fetcher.fetch(url)

            # Sitemap document: return <loc> links instantly, bypassing the
            # rendering and body-text extraction passes.
            if looks_like_sitemap(url, fast.content_type):
                response = self._build(
                    url,
                    fast,
                    title=None,
                    meta_description=None,
                    text="",
                    links=parse_sitemap(fast.html, max_links),
                    rendered=False,
                    block_suspected=False,
                )
                await self._cache.set(response, render_flag)
                return response

            decision = block_detector.quick_block(
                fast.html,
                fast.status,
                render_requested=(render == 1),
            )
            if decision.blocked:
                return await self._escalate(url, fast, max_text, max_links, render_flag)

            content = extract_content(
                fast.html, fast.final_url, max_text, max_links
            )
            thin = block_detector.thin_text_block(
                len(fast.html),
                len(content.text),
                self._settings.text_anomaly_html_bytes,
                self._settings.text_min_char_threshold,
            )
            if thin.blocked:
                return await self._escalate(url, fast, max_text, max_links, render_flag)

            response = self._build(
                url,
                fast,
                title=content.title,
                meta_description=content.meta_description,
                text=content.text,
                links=content.links,
                rendered=False,
                block_suspected=False,
            )
            await self._cache.set(response, render_flag)
            return response

    # -- internals --------------------------------------------------------------

    async def _escalate(
        self,
        url: str,
        fast: FastPathResult,
        max_text: int,
        max_links: int,
        render_flag: int,
    ) -> ScrapeResponse:
        """Render the page in the stealth browser pool and re-evaluate it."""
        outcome: RenderOutcome = await self._pool.render(url)
        html = outcome.html or ""
        final_url = outcome.final_url or fast.final_url
        status = outcome.status or fast.status

        content = extract_content(html, final_url, max_text, max_links)
        block_suspected = block_detector.quick_block(
            html, status, render_requested=False
        ).blocked
        if not block_suspected:
            block_suspected = block_detector.thin_text_block(
                len(html),
                len(content.text),
                self._settings.text_anomaly_html_bytes,
                self._settings.text_min_char_threshold,
            ).blocked

        response = self._build(
            url,
            fast,
            title=content.title,
            meta_description=content.meta_description,
            text=content.text,
            links=content.links,
            rendered=True,
            block_suspected=block_suspected,
            final_url=final_url,
            status_code=status,
        )
        await self._cache.set(response, render_flag)
        return response

    def _build(
        self,
        url: str,
        fast: FastPathResult,
        *,
        title: str | None,
        meta_description: str | None,
        text: str,
        links: list[LinkItem],
        rendered: bool,
        block_suspected: bool,
        final_url: str | None = None,
        status_code: int | None = None,
    ) -> ScrapeResponse:
        return ScrapeResponse(
            url=url,
            final_url=final_url or fast.final_url,
            status=status_code or fast.status,
            title=title,
            meta_description=meta_description,
            text=text,
            links=links,
            rendered=rendered,
            block_suspected=block_suspected,
            cached=False,
            age_seconds=0,
            content_type=fast.content_type,
        )