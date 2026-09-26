"""Dependency manifests must stay honest.

Guards two failures that actually shipped to a fresh clone:

* ``lxml_html_clean`` is required by the ``trafilatura -> justext ->
  lxml.html.clean`` import chain but is declared by *nobody* as a hard
  requirement - lxml ships it only as the optional extra ``html-clean`` - so a
  fresh install could fail at import time;
* the requirements file drifted to floating ``>=`` ranges, so installs were not
  reproducible.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _requirements() -> str:
    return (ROOT / "requirements.txt").read_text()


def test_requirements_pin_the_phantom_lxml_html_clean_dependency() -> None:
    assert re.search(r"^lxml_html_clean==\S+", _requirements(), re.M), (
        "lxml_html_clean missing: import trafilatura fails without it"
    )


def test_runtime_requirements_are_fully_pinned() -> None:
    """No floating ranges - a fresh install must reproduce a tested set."""
    loose = [
        line
        for line in _requirements().splitlines()
        if line.strip() and not line.startswith(("#", "-")) and "==" not in line
    ]
    assert not loose, f"unpinned requirements: {loose}"


def test_dev_requirements_are_fully_pinned() -> None:
    loose = [
        line
        for line in (ROOT / "requirements-dev.txt").read_text().splitlines()
        if line.strip() and not line.startswith(("#", "-")) and "==" not in line
    ]
    assert not loose, f"unpinned dev requirements: {loose}"


def test_pytest_config_lives_in_pyproject_not_a_stray_ini() -> None:
    """pytest.ini silently overrides pyproject.toml, which broke `make test`."""
    assert (ROOT / "pyproject.toml").exists()
    assert not (ROOT / "pytest.ini").exists(), "a stray pytest.ini overrides pyproject.toml"
    assert "[tool.pytest.ini_options]" in (ROOT / "pyproject.toml").read_text()
