from typing import Any
import pytest
from wifi_helper.diagnose import Action, IncidentState, Snapshot, Verdict, choose_action, classify
from wifi_helper.modem import MonitoringStatus
from wifi_helper.net import HomeCheck

HOME = HomeCheck(True, "gateway matches", "192.168.8.1", "aa:bb:cc:dd:ee:ff", None)
AWAY = HomeCheck(False, "gateway 10.0.0.1 is not ours", "10.0.0.1", None, None)


def modem(status="901", bars=4, sim=True, dns=("10.1.1.1",)) -> MonitoringStatus:
    return MonitoringStatus(status, bars, "4G LTE", dns[0] if dns else "", "", sim)


def snap(**kw) -> Snapshot:
    base: dict[str, Any] = dict(home=HOME, http="fail", ip_ok=False, dns_ok=False, modem_reachable=True, modem=modem(), bearer_ok=False)
    base.update(kw)
    return Snapshot(**base)


@pytest.mark.parametrize(
    "kw, expected",
    [
        (dict(home=AWAY), Verdict.NOT_HOME),
        (dict(http="ok"), Verdict.ONLINE),
        (dict(router_reachable=False), Verdict.LAN_DOWN),
        (dict(modem_reachable=False), Verdict.LAN_DOWN),
        (dict(router_reachable=True, modem_reachable=False), Verdict.MODEM_UNREACHABLE),
        (dict(modem=modem(sim=False)), Verdict.NO_SIGNAL),
        (dict(modem=modem(status="902", bars=0)), Verdict.NO_SIGNAL),
        (dict(modem=modem(status="902", bars=3)), Verdict.MODEM_DISCONNECTED),
        # The case this tool exists for: "connected", full bars, nothing flows.
        (dict(modem=modem(bars=5), bearer_ok=False), Verdict.DEAD_BEARER),
        (dict(bearer_ok=True, vpn_iface="utun3"), Verdict.VPN_PROBLEM),
        (dict(bearer_ok=True, vpn_iface="en0"), Verdict.UPSTREAM_DOWN),
        (dict(modem=None, bearer_ok=None), Verdict.DEAD_BEARER),
        (dict(bearer_ok=True, ip_ok=True, dns_ok=False), Verdict.DNS_BROKEN),
        (dict(bearer_ok=True, ip_ok=False, http="redirect"), Verdict.REDIRECTED),
        (dict(bearer_ok=True, ip_ok=True, dns_ok=True, http="redirect"), Verdict.REDIRECTED),
        (dict(bearer_ok=True, ip_ok=True, dns_ok=True, http="fail"), Verdict.HTTP_BLOCKED),
    ],
)
def test_classify(kw, expected):
    assert classify(snap(**kw)) == expected


def choose(verdict, state=None, router=True, last_reboot=0.0, now=10_000.0, cooldown=900):
    return choose_action(
        verdict, state or IncidentState(), router_configured=router, last_reboot_at=last_reboot, reboot_cooldown=cooldown, now=now
    )


def test_dead_bearer_ladder_cycles_twice_then_reboots_once():
    state = IncidentState()
    seen = []
    for _ in range(5):
        a = choose(Verdict.DEAD_BEARER, state)
        seen.append(a)
        if a == Action.NONE:
            break
        state.record(a, now=0)
    assert seen == [Action.CYCLE_DATA, Action.CYCLE_DATA, Action.REBOOT_MODEM, Action.NONE]


def test_reboot_respects_cooldown():
    state = IncidentState(cycles=2)
    assert choose(Verdict.DEAD_BEARER, state, last_reboot=9_500, now=10_000) == Action.NONE
    assert choose(Verdict.DEAD_BEARER, state, last_reboot=9_000, now=10_000) == Action.REBOOT_MODEM


def test_upstream_only_gets_one_cycle_and_no_reboot():
    state = IncidentState()
    assert choose(Verdict.UPSTREAM_DOWN, state) == Action.CYCLE_DATA
    state.record(Action.CYCLE_DATA, now=0)
    assert choose(Verdict.UPSTREAM_DOWN, state) == Action.NONE


@pytest.mark.parametrize(
    "verdict",
    [Verdict.NO_SIGNAL, Verdict.VPN_PROBLEM, Verdict.DNS_BROKEN, Verdict.REDIRECTED, Verdict.LAN_DOWN, Verdict.HTTP_BLOCKED],
)
def test_problems_a_reboot_cannot_fix_are_left_alone(verdict):
    assert choose(verdict) == Action.NONE


def test_unreachable_modem_reboots_router_only_if_configured():
    assert choose(Verdict.MODEM_UNREACHABLE) == Action.REBOOT_ROUTER
    assert choose(Verdict.MODEM_UNREACHABLE, router=False) == Action.NONE
    assert choose(Verdict.MODEM_UNREACHABLE, IncidentState(router_reboots=1)) == Action.NONE


def test_long_outage_allows_ladder_again():
    state = IncidentState()
    state.record(Action.CYCLE_DATA, now=1_000)
    assert not state.exhausted_for(900, now=1_500)
    assert state.exhausted_for(900, now=2_000)
    assert not IncidentState().exhausted_for(900, now=2_000)
