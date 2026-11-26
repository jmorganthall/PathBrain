"""SQM off raced in the ring (Josh, Nov 26: "just another profile", raced regularly, "data
back, not just an assumption"). An SQM off leg is a pipe switch: every pipe off, confirmed,
and turned back on before the next shaped leg, at the session's end, and after a reboot."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pathbrain import duel as duel_mod, firewall_write
from pathbrain.database import session_scope
from pathbrain.models import Duel, DuelStatus
from pathbrain.providers.base import ConfigProvider, FqCodelConfig
from pathbrain.settings_profile import SQM_OFF_FINGERPRINT, normalize


class Pipes(ConfigProvider):
    """Two pipes with uuids; records every on/off toggle."""

    name = "pipes"

    def __init__(self, fail_on: str | None = None) -> None:
        self.enabled = {"dl": True, "ul": True}
        self.toggles: list[tuple[str, bool]] = []
        self.fail_on = fail_on

    def discover(self) -> list[FqCodelConfig]:
        return [
            FqCodelConfig(quantum=1514, extra={"uuid": "dl", "description": "wan-download",
                                                "enabled": self.enabled["dl"]}),
            FqCodelConfig(quantum=300, extra={"uuid": "ul", "description": "wan-upload",
                                              "enabled": self.enabled["ul"]}),
        ]

    def snapshot(self) -> dict:
        return {}

    def set_pipe_enabled(self, pipe_uuid, enabled) -> dict:
        if pipe_uuid == self.fail_on and not enabled:
            raise RuntimeError("firewall said no")
        self.toggles.append((pipe_uuid, bool(enabled)))
        self.enabled[pipe_uuid] = bool(enabled)
        return {"ok": True}


def test_an_sqm_off_leg_turns_every_pipe_off_and_reads_as_sqm_off():
    p = Pipes()
    duel_mod._apply_unshaped(p)
    assert p.enabled == {"dl": False, "ul": False}


def test_a_half_unshaped_link_is_turned_back_on_and_the_leg_fails():
    p = Pipes(fail_on="ul")
    with pytest.raises(RuntimeError, match="could not turn shaping off"):
        duel_mod._apply_unshaped(p)
    assert p.enabled == {"dl": True, "ul": True}


def test_reshape_turns_back_on_only_what_the_session_found_on():
    p = Pipes()
    baseline = normalize(p.discover())
    firewall_write.unshape(p)
    assert firewall_write.reshape(p, baseline) == ["dl", "ul"]
    assert p.enabled == {"dl": True, "ul": True}
    assert firewall_write.reshape(p, baseline) == []      # nothing off: nothing written
    p.enabled["ul"] = False                                # a pipe the owner keeps off
    off_baseline = normalize(p.discover())
    p.enabled["dl"] = False
    firewall_write.reshape(p, off_baseline)
    assert p.enabled == {"dl": True, "ul": False}


def test_shaping_off_against_shaping_on_can_always_differ():
    shaped = normalize(Pipes().discover())
    off = [{**pipe, "enabled": False} for pipe in shaped]  # same inert fields, pipes off
    books = {"crown": {"settings": shaped}, SQM_OFF_FINGERPRINT: {"settings": off}}
    assert duel_mod._bout_cannot_differ(books, SQM_OFF_FINGERPRINT, "crown") is None


def _fought(s, days_ago: float, verdict: str = "incumbent"):
    s.add(Duel(status=DuelStatus.COMPLETE,
               finished_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
               matchups=[{"incumbent": "crown-x", "challenger": SQM_OFF_FINGERPRINT, "verdict": verdict}]))


def test_sqm_off_is_due_every_third_night_by_default():
    with session_scope() as s:
        s.query(Duel).delete()
        assert duel_mod.sqm_off_due(s, {})                 # never raced: due
        _fought(s, 1.0)
        s.flush()
        assert not duel_mod.sqm_off_due(s, {})            # raced last night
        assert not duel_mod.sqm_off_due(s, {"sqm_off_every_nights": 0})
        s.query(Duel).delete()
        _fought(s, 3.0)
        _fought(s, 0.5, verdict="aborted")                 # an abort is not a race
        s.flush()
        assert duel_mod.sqm_off_due(s, {})
        s.query(Duel).delete()


def test_a_reboot_mid_sqm_off_leg_turns_shaping_back_on(monkeypatch):
    p = Pipes()
    baseline = normalize(p.discover())
    firewall_write.unshape(p)
    monkeypatch.setattr(duel_mod, "get_provider", lambda: p)
    with session_scope() as s:
        s.add(Duel(status=DuelStatus.RUNNING, duration_s=60, baseline=baseline))
    duel_mod.reconcile_interrupted_duels()
    assert p.enabled == {"dl": True, "ul": True}
    with session_scope() as s:
        s.query(Duel).delete()
