"""Deployment manifests must not drift from the code.

Each assertion here encodes a bug that actually shipped to a fresh clone:

* the API port is written in four files and nothing connected them;
* ``muninn:latest`` is a mutable tag, so a build from another machine was
  silently reused on a different architecture and ``docker compose up`` failed
  with a platform mismatch;
* the healthcheck has to survive on an image with no ``curl``.

These are cheap to assert and expensive to rediscover in production.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def _compose() -> str:
    return (ROOT / "docker-compose.yml").read_text()


def _dockerfile() -> str:
    return (ROOT / "Dockerfile").read_text()


def test_compose_always_rebuilds_instead_of_trusting_a_stale_tag() -> None:
    """`image: muninn:latest` plus `build:` reuses whatever is cached.

    On a host whose architecture differs from the cached image, compose then
    refuses to start. Forcing a build makes the image always match ./.
    """
    compose = _compose()
    services = re.findall(r"^  (\S+):\n(.*?)(?=^  \S+:|\Z)", compose, re.S | re.M)
    assert services, "could not parse services out of docker-compose.yml"
    for name, body in services:
        assert "pull_policy: build" in body, f"{name} may reuse a stale foreign-arch image"


def test_compose_does_not_pin_an_architecture() -> None:
    """No hardcoded `platform:` - the stack must build natively on arm and amd."""
    assert "platform:" not in _compose(), (
        "a hardcoded platform breaks one of the two architectures; remove it and "
        "rely on a native build"
    )


def test_compose_has_no_obsolete_version_key() -> None:
    assert not re.search(r"^version:", _compose(), re.M)


def test_healthchecks_avoid_curl() -> None:
    """python:slim has no curl, so a curl healthcheck can never pass.

    Comments explaining that are fine; the executed command is what matters.
    """
    for path, text in (("docker-compose.yml", _compose()), ("Dockerfile", _dockerfile())):
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        assert "curl" not in code, f"{path} still probes with curl, which the image lacks"
        assert "urllib.request" in code, f"{path} should probe with the interpreter"


def test_container_does_not_run_as_root() -> None:
    assert re.search(r"^USER \S+", _dockerfile(), re.M), "Dockerfile has no USER directive"
    assert "USER root" not in _dockerfile()


def test_requirements_pin_the_phantom_lxml_html_clean_dependency() -> None:
    """lxml_html_clean is required by the trafilatura -> justext import chain but
    declared by nobody, so it must be pinned explicitly in requirements.txt."""
    reqs = (ROOT / "requirements.txt").read_text()
    assert re.search(r"^lxml_html_clean==\S+", reqs, re.M), (
        "lxml_html_clean missing: import trafilatura fails without it"
    )


def test_requirements_are_fully_pinned() -> None:
    """No floating ranges - a fresh install must reproduce a tested set."""
    loose = [
        line
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.startswith(("#", "-")) and "==" not in line
    ]
    assert not loose, f"unpinned requirements: {loose}"
