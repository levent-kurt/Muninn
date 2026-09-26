"""The listening port must agree everywhere it is written down.

The port appears in several independent places - ``Settings.port``, the
``Makefile`` and the documented ``uvicorn`` command - and nothing in the code
connects them. That is not theoretical: a bare ``uvicorn app.main:app``
silently used uvicorn's own default of 8000 while ``Settings.port`` said
otherwise, so the configured port was dead config nobody noticed.

These tests are the guard. If the port moves, they fail until every place moves
with it.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_default_port_is_9999() -> None:
    assert Settings().port == 9999


def test_makefile_uses_the_configured_port() -> None:
    makefile = (ROOT / "Makefile").read_text()
    match = re.search(r"\$\{PORT:-(\d+)\}", makefile)
    assert match, "Makefile run target should take the port from $${PORT:-<default>}"
    assert int(match.group(1)) == Settings().port


def test_readme_documents_the_configured_port() -> None:
    readme = (ROOT / "README.md").read_text()
    assert f"| `PORT` | `{Settings().port}` |" in readme
    assert f"127.0.0.1:{Settings().port}" in readme
    # No stale reference to the old default anywhere in the published docs.
    assert "8000" not in readme


def test_app_main_entry_point_uses_the_settings() -> None:
    """`python -m app.main` must honour HOST/PORT rather than uvicorn's own."""
    source = (ROOT / "app" / "main.py").read_text()
    assert "uvicorn.run(" in source
    assert "host=settings.host" in source
    assert "port=settings.port" in source


def test_service_units_agree_with_the_configured_port() -> None:
    """The shipped systemd/launchd examples must use the same port."""
    for name in ("deploy/muninn-api.service", "deploy/com.muninn.api.plist"):
        text = (ROOT / name).read_text()
        # systemd writes "--port 9999", launchd a separate <string> element, so
        # assert the invariant (right port, no stale default) rather than a format.
        assert str(Settings().port) in text, f"{name} does not use port {Settings().port}"
        assert "8000" not in text, f"{name} still references the old default port"
