"""Search result parsers for Google, Bing, DuckDuckGo, and Mojeek."""

from __future__ import annotations

from drivers.parsers.base import BaseParser, EngineBlockedError
from drivers.parsers.bing import BingParser
from drivers.parsers.ddg import DuckDuckGoParser
from drivers.parsers.google import GoogleParser
from drivers.parsers.mojeek import MojeekParser

__all__ = [
    "BaseParser",
    "EngineBlockedError",
    "GoogleParser",
    "BingParser",
    "DuckDuckGoParser",
    "MojeekParser",
]

# Engine name -> parser class registry (matches app.config.SUPPORTED_ENGINES).
PARSER_REGISTRY: dict[str, type[BaseParser]] = {
    GoogleParser.ENGINE_NAME: GoogleParser,
    BingParser.ENGINE_NAME: BingParser,
    DuckDuckGoParser.ENGINE_NAME: DuckDuckGoParser,
    MojeekParser.ENGINE_NAME: MojeekParser,
}


def get_parser(engine: str) -> type[BaseParser]:
    """Return the parser for ``engine`` or raise ``KeyError``."""
    try:
        return PARSER_REGISTRY[engine]
    except KeyError:
        raise KeyError(f"unsupported engine: {engine!r}") from None