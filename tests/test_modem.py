from contextlib import contextmanager
import subprocess
import pytest
import requests
from wifi_helper import modem, net
from wifi_helper.config import Settings
from wifi_helper.modem import ModemError

SETTINGS = Settings(_env_file=None, modem_password="x")


class FakeModem:
    def __init__(self, drop_on=(), login_fails=False):
        self.data = "1"
        self.calls = 0
        self.drop_on = set(drop_on)
        self.login_fails = login_fails
        self.sets: list[int] = []
        self.dial_up = self

    def _call(self):
        self.calls += 1
        if self.calls in self.drop_on:
            raise ModemError("Connection reset by peer")

    def set_mobile_dataswitch(self, value: int) -> str:
        self.calls += 1
        self.sets.append(value)
        self.data = str(value)
        if self.calls in self.drop_on:
            raise ModemError("Connection reset by peer")
        return "OK"

    def mobile_dataswitch(self) -> dict:
        self._call()
        return {"dataswitch": self.data}


@pytest.fixture
def fake_modem(monkeypatch):
    monkeypatch.setattr(modem.time, "sleep", lambda s: None)

    def install(**kw) -> FakeModem:
        m = FakeModem(**kw)

        @contextmanager
        def session(s, login=True, timeout=15.0):
            if m.login_fails:
                raise ModemError("Read timed out")
            yield m

        monkeypatch.setattr(modem, "session", session)
        return m
    return install


def test_cycle_data_turns_data_off_then_on(fake_modem):
    m = fake_modem()
    assert modem.cycle_data(SETTINGS)
    assert m.sets == [0, 1] and m.data == "1"


def test_cycle_data_turns_data_back_on_when_connection_drops_after_off(fake_modem):
    m = fake_modem(drop_on={1})
    assert modem.cycle_data(SETTINGS)
    assert m.sets[0] == 0 and m.data == "1"


def test_cycle_data_turns_data_back_on_when_the_on_reply_is_lost(fake_modem):
    m = fake_modem(drop_on={2, 3})
    assert modem.cycle_data(SETTINGS)
    assert m.data == "1"


def test_cycle_data_reports_failure_before_touching_data(fake_modem):
    m = fake_modem(login_fails=True)
    with pytest.raises(ModemError, match="Read timed out"):
        modem.cycle_data(SETTINGS)
    assert m.sets == []


def test_ensure_data_on_gives_up_loudly(fake_modem):
    m = fake_modem(drop_on=set(range(1, 100)))
    m.data = "0"
    with pytest.raises(ModemError, match="may still be off"):
        modem.ensure_data_on(SETTINGS, tries=3)


class FakeConnection:
    def __init__(self, *a, **kw):
        pass

    def close(self):
        raise requests.ConnectionError("Remote end closed connection without response")


def test_failed_logout_does_not_hide_the_result(monkeypatch):
    monkeypatch.setattr(modem, "Connection", FakeConnection)
    with modem.session(SETTINGS) as c:
        result = "rebooting"
    assert result == "rebooting" and c is not None


def test_errors_in_the_body_still_become_modem_errors(monkeypatch):
    monkeypatch.setattr(modem, "Connection", FakeConnection)
    with pytest.raises(ModemError, match="Read timed out"):
        with modem.session(SETTINGS):
            raise requests.ReadTimeout("Read timed out")


@pytest.mark.skipif(not subprocess._USE_POSIX_SPAWN, reason="platform has no posix_spawn path")
def test_commands_are_spawned_without_forking(monkeypatch):
    spawned = []
    real = subprocess.Popen._posix_spawn
    monkeypatch.setattr(subprocess.Popen, "_posix_spawn", lambda self, args, *a: spawned.append(args) or real(self, args, *a))
    assert net._run(["echo", "hi"]) == "hi\n"
    assert spawned and spawned[0][0].endswith("/echo")


def test_missing_command_returns_empty():
    assert net._run(["no-such-command-wifi-helper"]) == ""
