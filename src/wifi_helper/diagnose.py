"""Find which hop is broken, then pick the least disruptive fix for that hop.

Path:  this computer -> [Wi-Fi router] -> modem -> cell tower / carrier core -> internet

Each probe answers one question about one hop. `classify` and `choose_action`
are pure functions so the decision logic can be tested without hardware.
"""

from dataclasses import dataclass, field
from enum import Enum
import time
from . import net
from .config import Settings
from .modem import ModemClient, MonitoringStatus


class Verdict(str, Enum):
    ONLINE = "online"
    NOT_HOME = "not_home"
    LAN_DOWN = "lan_down"
    MODEM_UNREACHABLE = "modem_unreachable"
    NO_SIGNAL = "no_signal"
    MODEM_DISCONNECTED = "modem_disconnected"
    DEAD_BEARER = "dead_bearer"
    VPN_PROBLEM = "vpn_problem"
    UPSTREAM_DOWN = "upstream_down"
    DNS_BROKEN = "dns_broken"
    REDIRECTED = "redirected"
    HTTP_BLOCKED = "http_blocked"


class Action(str, Enum):
    NONE = "none"
    CYCLE_DATA = "cycle_data"
    REBOOT_MODEM = "reboot_modem"
    REBOOT_ROUTER = "reboot_router"


EXPLANATIONS = {
    Verdict.ONLINE: "Internet is working.",
    Verdict.NOT_HOME: "Not on your home network, so nothing will be touched.",
    Verdict.LAN_DOWN: "This computer can't reach its own gateway. Check the Wi-Fi connection on this device.",
    Verdict.MODEM_UNREACHABLE: "The router is up but the modem doesn't answer. The router-to-modem link or the modem itself is stuck.",
    Verdict.NO_SIGNAL: "The modem has no usable signal or SIM. Restarting won't help; check SIM, coverage, or wait for the tower.",
    Verdict.MODEM_DISCONNECTED: "The modem has signal but no data session.",
    Verdict.DEAD_BEARER: "The modem says 'connected' but nothing crosses the cellular link (the carrier's own DNS is silent). The data session is stale.",
    Verdict.VPN_PROBLEM: "The cellular link works (the carrier answers) but traffic through your VPN doesn't. The VPN is the problem, not the modem.",
    Verdict.UPSTREAM_DOWN: "The carrier answers but the public internet doesn't. Likely a carrier-side outage; a fresh data session sometimes helps.",
    Verdict.DNS_BROKEN: "Raw internet works but name lookups fail. Change DNS on this device or router; the modem is fine.",
    Verdict.REDIRECTED: "Web requests are being redirected. Usually means your data bundle ran out or the carrier wants you to accept a page. Check your balance.",
    Verdict.HTTP_BLOCKED: "IP and DNS work but web checks fail. Something on the path (proxy, firewall, MTU) is interfering.",
}


@dataclass
class Snapshot:
    home: net.HomeCheck
    http: str = "fail"
    ip_ok: bool = False
    dns_ok: bool = False
    vpn_iface: str | None = None
    router_reachable: bool | None = None
    modem_reachable: bool | None = None
    modem: MonitoringStatus | None = None
    modem_error: str | None = None
    bearer_ok: bool | None = None
    probe_seconds: float = 0.0

    @property
    def vpn_active(self) -> bool:
        return net.is_vpn_interface(self.vpn_iface)


def classify(s: Snapshot) -> Verdict:
    if not s.home.is_home:
        return Verdict.NOT_HOME
    if s.http == "ok":
        return Verdict.ONLINE
    if s.router_reachable is False:
        return Verdict.LAN_DOWN
    if s.modem_reachable is False:
        return Verdict.LAN_DOWN if s.router_reachable is None else Verdict.MODEM_UNREACHABLE
    if s.modem is not None:
        if s.modem.sim_ok is False or (not s.modem.connected and s.modem.signal_bars == 0):
            return Verdict.NO_SIGNAL
        if not s.modem.connected:
            return Verdict.MODEM_DISCONNECTED
    if s.http == "redirect":
        return Verdict.REDIRECTED
    if s.bearer_ok is False:
        return Verdict.DEAD_BEARER
    if not s.ip_ok:
        if s.vpn_active and s.bearer_ok:
            return Verdict.VPN_PROBLEM
        if s.bearer_ok:
            return Verdict.UPSTREAM_DOWN
        # Bearer unknown (modem API unavailable): a stale session is the common case.
        return Verdict.DEAD_BEARER
    if not s.dns_ok:
        return Verdict.DNS_BROKEN
    return Verdict.HTTP_BLOCKED


@dataclass
class IncidentState:
    """What has already been tried during the current outage."""

    cycles: int = 0
    modem_reboots: int = 0
    router_reboots: int = 0
    actions: list[str] = field(default_factory=list)
    last_action_at: float = 0.0

    def record(self, action: "Action", now: float | None = None) -> None:
        self.actions.append(action.value)
        self.last_action_at = time.time() if now is None else now
        if action == Action.CYCLE_DATA:
            self.cycles += 1
        elif action == Action.REBOOT_MODEM:
            self.modem_reboots += 1
        elif action == Action.REBOOT_ROUTER:
            self.router_reboots += 1

    def exhausted_for(self, seconds: float, now: float | None = None) -> bool:
        """True once something was tried and nothing has been tried for `seconds`."""
        now = time.time() if now is None else now
        return bool(self.actions) and now - self.last_action_at >= seconds


MAX_CYCLES = {Verdict.MODEM_DISCONNECTED: 2, Verdict.DEAD_BEARER: 2, Verdict.UPSTREAM_DOWN: 1}
CAN_REBOOT_MODEM = {Verdict.MODEM_DISCONNECTED, Verdict.DEAD_BEARER}


def choose_action(
    verdict: Verdict,
    incident: IncidentState,
    *,
    router_configured: bool,
    last_reboot_at: float,
    reboot_cooldown: int,
    now: float,
) -> Action:
    reboot_allowed = now - last_reboot_at >= reboot_cooldown
    if verdict == Verdict.MODEM_UNREACHABLE:
        if router_configured and incident.router_reboots == 0 and reboot_allowed:
            return Action.REBOOT_ROUTER
        return Action.NONE
    if incident.cycles < MAX_CYCLES.get(verdict, 0):
        return Action.CYCLE_DATA
    if verdict in CAN_REBOOT_MODEM and incident.modem_reboots == 0 and reboot_allowed:
        return Action.REBOOT_MODEM
    return Action.NONE


def quick_check(settings: Settings) -> Snapshot:
    """The cheap check run every interval: are we home, and does the web work?"""
    home = net.check_home_network(settings)
    snap = Snapshot(home=home)
    if home.is_home:
        snap.http = net.http_check()
    return snap


def full_check(settings: Settings, snap: Snapshot | None = None) -> Snapshot:
    """Probe every hop. Only runs when something is wrong (or on `wifi doctor`)."""
    started = time.monotonic()
    snap = snap or quick_check(settings)
    if not snap.home.is_home:
        return snap
    if snap.http != "ok":
        snap.http = net.http_check()

    snap.vpn_iface = net.route_interface("1.1.1.1")
    snap.ip_ok = net.ip_reachable()
    snap.dns_ok = net.system_dns_ok()

    if settings.router_configured and settings.router_host != settings.modem_host:
        snap.router_reachable = net.tcp_reachable(settings.router_host, 80)
    snap.modem_reachable = net.tcp_reachable(settings.modem_host, 80)

    if snap.modem_reachable:
        try:
            with ModemClient(timeout=6.0) as m:
                snap.modem = m.monitoring_status()
        except Exception as e:  # any failure here just means "modem state unknown"
            snap.modem_error = str(e) or e.__class__.__name__

    if snap.modem and snap.modem.carrier_dns:
        targets = snap.modem.carrier_dns
        # If even the carrier's private resolver is routed into a VPN, the probe proves nothing.
        if not net.is_vpn_interface(net.route_interface(targets[0])):
            snap.bearer_ok = any(net.dns_server_answers(d) for d in targets)

    snap.probe_seconds = time.monotonic() - started
    return snap
