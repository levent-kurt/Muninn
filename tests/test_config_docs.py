"""The README's configuration table must match the code.

The docs list roughly sixty environment variables in two places - the README
and ``app/config.py`` - with nothing tying them together. That has already
drifted once: the endpoint advertised ``max_text`` up to 32000 while the service
silently clamped every request to ``default_max_text``, making the documented
bound unreachable.

These tests make drift a build failure rather than a support ticket.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]

# Env vars read by a hand-rolled helper or built into an alias, so they cannot
# be discovered by scanning for _env_*("NAME").
_DOCUMENTED_ALIASES = {"HOST", "PORT", "BROWSER_ARGS", "USER_AGENT", "LOCALE"}


def _env_vars_in_code() -> set[str]:
    source = (ROOT / "app" / "config.py").read_text()
    found = set(re.findall(r'_env_\w+\(\s*"([A-Z0-9_]+)"', source))
    found |= set(re.findall(r'os\.environ\.get\(\s*"([A-Z0-9_]+)"', source))
    return found


def _documented_in_readme() -> set[str]:
    readme = (ROOT / "README.md").read_text()
    # Only the configuration tables: `| `NAME` | `default` | description |`
    return set(re.findall(r"^\|\s*`([A-Z0-9_]+)`\s*\|", readme, re.M))


def test_every_env_var_is_documented_in_the_readme() -> None:
    code = _env_vars_in_code()
    documented = _documented_in_readme()
    missing = sorted(code - documented)
    assert not missing, f"undocumented environment variables: {missing}"


def test_readme_documents_no_env_var_that_does_not_exist() -> None:
    """A renamed or deleted variable must not linger in the docs."""
    code = _env_vars_in_code() | _DOCUMENTED_ALIASES
    stale = sorted(_documented_in_readme() - code)
    assert not stale, f"documented but not read by the code: {stale}"


def test_documented_defaults_match_the_code() -> None:
    """The default column in the README must be the real default."""
    readme = (ROOT / "README.md").read_text()
    rows = re.findall(r"^\|\s*`([A-Z0-9_]+)`\s*\|\s*`?([^`|]*)`?\s*\|", readme, re.M)
    source = (ROOT / "app" / "config.py").read_text()
    mismatches: list[str] = []
    for name, documented_default in rows:
        if name in _DOCUMENTED_ALIASES:
            continue
        # Find this setting's default in the dataclass source.
        pattern = rf"{name}\s*:.*?default_factory=lambda: _env_\w+\(\s*\"{name}\",\s*([^)]*)\)"
        match = re.search(pattern, source, re.S)
        if match is None:
            continue
        raw = match.group(1).strip()
        documented = documented_default.strip().strip("`")
        # Normalise thousands separators and float spelling.
        a = documented.replace(",", "").rstrip("0").rstrip(".").lower()
        b = raw.replace(",", "").replace("_", "").rstrip("0").rstrip(".").lower()
        if a and b and a != b:
            mismatches.append(f"{name}: README={documented!r} code={raw!r}")
    assert not mismatches, "default value drift:\n  " + "\n  ".join(mismatches)


def test_documented_port_matches_the_settings() -> None:
    assert f"`{Settings().port}`" in (ROOT / "README.md").read_text()
