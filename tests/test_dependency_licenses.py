"""Runtime dependencies must be permissively licensed.

REPORT2 listed "confirm every dependency permits the chosen license" as an open
publishing blocker. MIT-licensed Muninn can absorb MIT/BSD/Apache/ISC/PSF
dependencies; anything copyleft or unknown needs a deliberate decision, so it is
caught here rather than after publication.

The check runs against the *installed* metadata, so it also fails if someone
swaps a pin for a differently-licensed fork.
"""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Licences compatible with distributing this project under MIT. The values are
# the SPDX ids / classifier strings that actually appear in the installed
# metadata, lower-cased; matching is substring-based in both directions so
# "BSD-3-Clause" and "bsd" both hit.
ALLOWED_LICENSES = {
    "mit",
    "mit license",
    "bsd",
    "bsd-2-clause",
    "bsd-3-clause",
    "apache",
    "apache-2.0",
    "apache software license",
    "isc",
    "psf",
    "python software foundation license",
    "mpl-2.0",  # certifi (Mozilla Public License 2.0): file-level, no copyleft effect
    "mozilla public license 2.0",
    "unlicense",
    "zlib",
}


def _runtime_requirements() -> list[str]:
    names: list[str] = []
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "-")):
            names.append(line.split("==")[0].split("[")[0].strip())
    return names


def _license_of(dist_name: str) -> str:
    meta = metadata.metadata(dist_name)
    expr = meta.get("License-Expression")
    if expr:
        return expr
    licence = meta.get("License") or ""
    if not licence or "see license" in licence.lower() or len(licence) > 200:
        # Fall back to the classifier set when License is a pointer or a blob.
        for classifier in meta.get_all("Classifier") or []:
            if classifier.startswith("License ::"):
                return classifier.split("::")[-1].strip()
    return licence


@pytest.mark.parametrize("dist", _runtime_requirements())
def test_runtime_dependency_license_is_permissive(dist: str) -> None:
    try:
        raw = _license_of(dist)
    except metadata.PackageNotFoundError:  # pragma: no cover - env problem
        pytest.skip(f"{dist} is not installed in this environment")

    normalised = raw.strip().lower()
    assert any(
        allowed in normalised or normalised in allowed for allowed in ALLOWED_LICENSES
    ), (
        f"{dist} is licensed {raw!r}, which is not in the permissive allowlist. "
        f"Either it is compatible (add it with a reason) or it is not."
    )


def test_the_allowlist_contains_no_copyleft() -> None:
    """Guard against a strong copyleft licence creeping into the allowlist.

    MPL-2.0 (certifi) is file-level weak copyleft with no distribution effect,
    so it is fine alongside MIT; the GPL family is not, and must never be added
    silently.
    """
    for name in ALLOWED_LICENSES:
        assert "gpl" not in name, f"{name} is copyleft; adding it needs a deliberate decision"


# Extras pull in further packages, so their licences are not covered by the
# per-requirement check above. uvicorn[standard] is the only one we use; its
# transitive dependencies (uvloop, httptools, websockets, watchfiles, PyYAML,
# python-dotenv, colorama) are all MIT/BSD.
ALLOWED_REQUIREMENT_EXTRAS = {"uvicorn": {"standard"}}


def test_only_documented_install_extras_are_used() -> None:
    """`pip install x[extra]` can pull in a different licence, so it must be a
    recorded decision rather than something that slipped into a pin."""
    used: dict[str, set[str]] = {}
    for match in re.finditer(
        r"^([a-z0-9_.-]+)\[([^]]+)\]", (ROOT / "requirements.txt").read_text(), re.M
    ):
        used.setdefault(match.group(1).lower(), set()).update(
            e.strip().lower() for e in match.group(2).split(",")
        )
    for name, extras in used.items():
        assert name in ALLOWED_REQUIREMENT_EXTRAS, f"{name}[...] is not documented"
        unknown = extras - ALLOWED_REQUIREMENT_EXTRAS[name]
        assert not unknown, f"{name}: undocumented extras {sorted(unknown)}"
