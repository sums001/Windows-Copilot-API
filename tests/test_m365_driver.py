"""Unit tests for M365Copilot.ask() retry/timeout behavior (no network).

The wedge this pins down: a Trouter endpoint registration going stale makes
every turn time out with *zero* deliveries. ask() must retry exactly once on a
fresh socket in that case, must NOT retry when frames did arrive (the route
works; the turn just never completed), and must surface a clear error either
way.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from copilot.m365 import driver
from copilot.m365.driver import M365Copilot


class FakeAuth:
    tenant = "tenant-guid"
    oid = "oid-guid"

    def token(self, scope):
        return f"token-{scope}"


class FakeReceiver:
    """Scripted TrouterReceiver: each instance pops the next (reply, deliveries)."""

    script = []       # class-level: [(reply, deliveries), ...]
    instances = []

    def __init__(self, ic3_token, proxy=None, verbose=False):
        FakeReceiver.instances.append(self)
        self.reply, self._deliveries = FakeReceiver.script.pop(0)
        self.sends = 0

    def receive(self, on_connected, timeout):
        on_connected({})
        self.sends += 1
        return self.reply


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(driver, "TrouterReceiver", FakeReceiver)
    FakeReceiver.instances = []
    b = M365Copilot(auth=FakeAuth(), timeout=1)
    # ask() fires the real HTTP send in on_connected; stub it out.
    monkeypatch.setattr(b, "_send", lambda *a, **k: None)
    return b


def test_ask_returns_reply_first_attempt(bot):
    FakeReceiver.script = [("hello back", 1)]
    assert bot.ask("hi") == "hello back"
    assert len(FakeReceiver.instances) == 1


def test_ask_retries_once_on_zero_deliveries(bot):
    # First socket sees nothing (stale registration); second works.
    FakeReceiver.script = [("", 0), ("recovered", 1)]
    assert bot.ask("hi") == "recovered"
    assert len(FakeReceiver.instances) == 2


def test_ask_gives_up_after_two_silent_attempts(bot):
    FakeReceiver.script = [("", 0), ("", 0)]
    with pytest.raises(RuntimeError, match="deliveries=0, attempts=2"):
        bot.ask("hi")
    assert len(FakeReceiver.instances) == 2


def test_ask_does_not_retry_when_frames_arrived(bot):
    # Deliveries came in but the turn never completed: a duplicate send won't
    # finish it faster, so exactly one attempt.
    FakeReceiver.script = [("", 3)]
    with pytest.raises(RuntimeError, match="deliveries=3, attempts=1"):
        bot.ask("hi")
    assert len(FakeReceiver.instances) == 1


def test_timeout_env_override(monkeypatch):
    monkeypatch.setenv("M365_TIMEOUT", "42")
    assert M365Copilot(auth=FakeAuth()).timeout == 42


def test_timeout_default(monkeypatch):
    monkeypatch.delenv("M365_TIMEOUT", raising=False)
    assert M365Copilot(auth=FakeAuth()).timeout == 180
