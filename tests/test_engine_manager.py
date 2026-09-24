"""Unit tests for the EngineManager circuit breaker and Round-Robin logic."""

from __future__ import annotations

import time

import pytest

from app.config import Settings
from app.engine_manager import AllEnginesQuarantinedError, EngineManager


@pytest.fixture
def settings() -> Settings:
    return Settings(
        quarantine_first_seconds=1_800,
        quarantine_escalated_seconds=43_200,
    )


@pytest.fixture
def manager(settings: Settings) -> EngineManager:
    return EngineManager(settings)


def _expire_all(manager: EngineManager) -> None:
    """Simulate every quarantine cooldown expiring (move clock forward)."""
    for st in manager._states.values():
        st.quarantined_until = min(st.quarantined_until or 0, time.time() - 1)


# --------------------------------------------------------------------------- initial state


async def test_all_engines_start_active(manager: EngineManager) -> None:
    assert set(manager.active_engines()) == {"google", "bing", "ddg", "mojeek"}
    status = await manager.status()
    assert all(v["status"] == "active" for v in status.values())


# --------------------------------------------------------------------------- quarantines


async def test_first_failure_quarantines_30min(manager: EngineManager) -> None:
    await manager.report_failure("google", "429")
    st = manager._states["google"]
    assert st.fail_count == 1
    assert st.quarantine_level == 1
    assert st.active is False
    assert st.remaining_cooldown() <= 1_800
    # engine is excluded from rotation
    assert "google" not in manager.active_engines()
    # others remain available
    assert set(manager.active_engines()) == {"bing", "ddg", "mojeek"}


async def test_second_consecutive_failure_escalates_to_12h(manager: EngineManager) -> None:
    await manager.report_failure("bing", "captcha")
    _expire_all(manager)  # cooldown elapses...
    await manager.report_failure("bing", "429")  # ...and it fails again on first retry
    st = manager._states["bing"]
    assert st.fail_count == 2
    assert st.quarantine_level == 2
    assert st.remaining_cooldown() <= 43_200
    # severity kicked in: cooldown is now much longer than the first level
    assert st.remaining_cooldown() > 1_800


async def test_success_resets_failure_counter(manager: EngineManager) -> None:
    await manager.report_failure("ddg", "captcha")
    await manager.report_success("ddg")  # impossible in reality while quarantined,
    # but models "success after cooldown" reset semantics
    st = manager._states["ddg"]
    assert st.fail_count == 0
    assert st.quarantine_level == 0
    assert st.active is True
    assert st.success_count == 1


async def test_escalation_requires_consecutive_failures(manager: EngineManager) -> None:
    # failure -> success -> failure must stay at the first quarantine level
    await manager.report_failure("mojeek", "429")
    await manager.report_success("mojeek")
    await manager.report_failure("mojeek", "429")
    st = manager._states["mojeek"]
    assert st.fail_count == 1
    assert st.quarantine_level == 1
    assert st.remaining_cooldown() <= 1_800


async def test_all_quarantined_raises(manager: EngineManager) -> None:
    for engine in ("google", "bing", "ddg", "mojeek"):
        await manager.report_failure(engine, "captcha")
    assert manager.active_engines() == []
    with pytest.raises(AllEnginesQuarantinedError):
        await manager.resolve_engine()


# --------------------------------------------------------------------------- round robin


async def test_round_robin_over_active_engines(manager: EngineManager) -> None:
    picked = [await manager.resolve_engine() for _ in range(8)]
    # 8 picks over 4 engines -> every engine exactly twice, in RR order
    assert picked == ["google", "bing", "ddg", "mojeek", "google", "bing", "ddg", "mojeek"]


async def test_round_robin_skips_quarantined(manager: EngineManager) -> None:
    await manager.report_failure("google", "captcha")
    picked = [await manager.resolve_engine() for _ in range(6)]
    assert "google" not in picked
    assert picked.count("bing") == 2
    assert picked.count("ddg") == 2
    assert picked.count("mojeek") == 2


async def test_requested_active_engine_is_honoured(manager: EngineManager) -> None:
    assert await manager.resolve_engine("bing") == "bing"


async def test_requested_quarantined_engine_is_ignored(manager: EngineManager) -> None:
    await manager.report_failure("ddg", "429")
    picked = await manager.resolve_engine("ddg")
    assert picked != "ddg"