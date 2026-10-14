from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
import time
from . import modem, net
from .config import Settings
from .modem import ModemError, MonitoringStatus


class Verdict(str, Enum):
    ONLINE = "online"
    NOT_HOME = "not_home"
    LAN_DOWN = "lan_down"
    MODEM_UNREACHABLE = "modem_unreachable"
    NO_SIGNAL = "no_signal"
    MODEM_CONNECTING = "modem_connecting"
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
    Verdict.MODEM_CONNECTING: "The modem is still connecting (normal for a minute or two after power-up or a data restart). Waiting before doing anything.",
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
    if s.router_reachable is False and not s.modem_reachable:
        return Verdict.LAN_DOWN
    if s.modem_reachable is False:
        return Verdict.LAN_DOWN if s.router_reachable is None else Verdict.MODEM_UNREACHABLE
    if s.modem is not None:
        if s.modem.sim_ok is False or (not s.modem.connected and s.modem.signal_bars == 0):
            return Verdict.NO_SIGNAL
        if s.modem.connection_status == "900":
            return Verdict.MODEM_CONNECTING
        if not s.modem.connected:
            return Verdict.MODEM_DISCONNECTED
    if s.http == "redirect" and (s.ip_ok or s.bearer_ok):
        return Verdict.REDIRECTED
    if s.bearer_ok is False:
        return Verdict.DEAD_BEARER
    if not s.ip_ok:
        if s.vpn_active and s.bearer_ok:
            return Verdict.VPN_PROBLEM
        if s.bearer_ok:
            return Verdict.UPSTREAM_DOWN
        return Verdict.DEAD_BEARER
    if not s.dns_ok:
        return Verdict.DNS_BROKEN
    return Verdict.HTTP_BLOCKED


@dataclass
class IncidentState:
    cycles: int = 0
    modem_reboots: int = 0
    router_reboots: int = 0
    actions: list[str] = field(default_factory=list)
    last_action_at: float = 0.0

    def record(self, action: Action, now: float | None = None) -> None:
        self.actions.append(action.value)
        self.last_action_at = time.time() if now is None else now
        if action == Action.CYCLE_DATA:
            self.cycles += 1
        elif action == Action.REBOOT_MODEM:
            self.modem_reboots += 1
        elif action == Action.REBOOT_ROUTER:
            self.router_reboots += 1

    def exhausted_for(self, seconds: float, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return bool(self.actions) and now - self.last_action_at >= seconds


MAX_CYCLES = {Verdict.MODEM_DISCONNECTED: 2, Verdict.DEAD_BEARER: 2, Verdict.UPSTREAM_DOWN: 1}
CAN_REBOOT_MODEM = {Verdict.MODEM_DISCONNECTED, Verdict.DEAD_BEARER}
CONNECTING_GRACE = 180
SETTLE_AFTER_ACTION = 120
MODEM_UNREACHABLE_GRACE = 300


def choose_action(
    verdict: Verdict,
    incident: IncidentState,
    *,
    separate_router: bool,
    last_reboot_at: float,
    reboot_cooldown: int,
    now: float,
    outage_seconds: float = 0.0,
) -> Action:
    reboot_allowed = now - last_reboot_at >= reboot_cooldown
    since_action = now - incident.last_action_at if incident.actions else float("inf")
    if verdict == Verdict.MODEM_CONNECTING:
        if outage_seconds < CONNECTING_GRACE or since_action < SETTLE_AFTER_ACTION:
            return Action.NONE
        verdict = Verdict.MODEM_DISCONNECTED
    if verdict == Verdict.MODEM_UNREACHABLE:
        if (
            separate_router
            and incident.router_reboots == 0
            and reboot_allowed
            and outage_seconds >= MODEM_UNREACHABLE_GRACE
            and since_action >= MODEM_UNREACHABLE_GRACE
        ):
            return Action.REBOOT_ROUTER
        return Action.NONE
    if incident.cycles < MAX_CYCLES.get(verdict, 0):
        return Action.CYCLE_DATA
    if verdict in CAN_REBOOT_MODEM and incident.modem_reboots == 0 and reboot_allowed:
        return Action.REBOOT_MODEM
    return Action.NONE


def quick_check(settings: Settings) -> Snapshot:
    snap = Snapshot(home=net.check_home_network(settings))
    if snap.home.is_home:
        snap.http = net.http_check()
    return snap


def _read_modem(settings: Settings) -> tuple[MonitoringStatus | None, str | None]:
    try:
        return modem.monitoring_status(settings), None
    except ModemError as e:
        return None, str(e)


def full_check(settings: Settings, snap: Snapshot | None = None) -> Snapshot:
    started = time.monotonic()
    snap = snap or quick_check(settings)
    if not snap.home.is_home:
        return snap
    with ThreadPoolExecutor() as pool:
        http = pool.submit(net.http_check) if snap.http != "ok" else None
        vpn_iface = pool.submit(net.route_interface, "1.1.1.1")
        ip_ok = pool.submit(net.ip_reachable)
        dns_ok = pool.submit(net.system_dns_ok)
        router_ok = pool.submit(net.tcp_reachable, settings.router_host, 80, 3.0) if settings.separate_router else None
        modem_ok = pool.submit(net.tcp_reachable, settings.modem_host, 80, 3.0)
        modem_state = pool.submit(_read_modem, settings)
    snap.http = http.result() if http else snap.http
    snap.vpn_iface, snap.ip_ok, snap.dns_ok = vpn_iface.result(), ip_ok.result(), dns_ok.result()
    snap.router_reachable = router_ok.result() if router_ok else None
    snap.modem_reachable = modem_ok.result()
    snap.modem, snap.modem_error = modem_state.result()
    if snap.modem and snap.modem.carrier_dns and not net.is_vpn_interface(net.route_interface(snap.modem.carrier_dns[0])):
        snap.bearer_ok = any(net.dns_server_answers(d) for d in snap.modem.carrier_dns)
    snap.probe_seconds = time.monotonic() - started
    return snap
