"""Low-level network probes. Nothing here needs a login or the internet."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout, as_completed
from dataclasses import dataclass
import re
import secrets
import socket
import struct
import subprocess
import sys
import httpx
from .config import Settings, normalize_mac

VPN_IFACE_PREFIXES = ("utun", "tun", "tap", "wg", "ppp", "ipsec", "cloudflarewarp", "tailscale", "zt")

# Endpoints that answer plain HTTP with an empty 204. Anything else (200, 30x)
# means something in the path is rewriting traffic: a captive or "out of data" page.
HTTP_204_URLS = (
    "http://connectivitycheck.gstatic.com/generate_204",
    "http://cp.cloudflare.com/generate_204",
)
# Anycast addresses used for a raw TCP reachability check that skips DNS.
IP_PROBE_TARGETS = (("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 443))
DNS_PROBE_NAME = "connectivitycheck.gstatic.com"


def _run(cmd: list[str], timeout: float = 2.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def get_default_gateway() -> str | None:
    if sys.platform == "darwin":
        for line in _run(["netstat", "-rn", "-f", "inet"]).splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "default" and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", parts[1]):
                return parts[1]
        return None
    m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", _run(["ip", "-4", "route", "show", "default"]))
    return m.group(1) if m else None


def _scutil(key: str) -> str:
    try:
        return subprocess.run(["scutil"], input=f"show {key}\n", capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return ""


def get_gateway_mac(ip: str) -> str | None:
    if sys.platform == "darwin":
        # configd records the router's MAC for every service. Unlike `arp`, this
        # stays readable for processes macOS's local-network privacy rules hide ARP from.
        svc = re.search(r"PrimaryService : (\S+)", _scutil("State:/Network/Global/IPv4"))
        if svc:
            info = _scutil(f"State:/Network/Service/{svc.group(1)}/IPv4")
            m_ip = re.search(r"ARPResolvedIPAddress : (\S+)", info)
            m_mac = re.search(r"ARPResolvedHardwareAddress : (\S+)", info)
            if m_ip and m_mac and m_ip.group(1) == ip:
                return normalize_mac(m_mac.group(1))
        m = re.search(r" at ([0-9a-fA-F:]+) ", _run(["arp", "-n", ip]))
    else:
        m = re.search(r"lladdr ([0-9a-fA-F:]+)", _run(["ip", "neigh", "show", ip]))
    return normalize_mac(m.group(1)) if m else None


def get_current_ssid() -> str | None:
    if sys.platform == "darwin":
        out = _run(["networksetup", "-getairportnetwork", "en0"])
        if "Current Wi-Fi Network:" in out:
            return out.split(":", 1)[1].strip() or None
        m = re.search(r"\n\s*SSID\s*:\s*(.+)", _run(["ipconfig", "getsummary", "en0"]))
        if m and "<redacted>" not in m.group(1):
            return m.group(1).strip() or None
        return None
    out = _run(["iwgetid", "-r"]).strip()
    if out:
        return out
    for line in _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]).splitlines():
        if line.startswith("yes:"):
            return line[4:] or None
    return None


def route_interface(ip: str) -> str | None:
    if sys.platform == "darwin":
        m = re.search(r"interface:\s*(\S+)", _run(["route", "-n", "get", ip]))
    else:
        m = re.search(r" dev (\S+)", _run(["ip", "route", "get", ip]))
    return m.group(1) if m else None


def is_vpn_interface(iface: str | None) -> bool:
    return bool(iface) and iface.lower().startswith(VPN_IFACE_PREFIXES)


def tcp_reachable(host: str, port: int = 80, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ip_reachable(timeout: float = 2.0) -> bool:
    with ThreadPoolExecutor(len(IP_PROBE_TARGETS)) as pool:
        return any(pool.map(lambda t: tcp_reachable(t[0], t[1], timeout), IP_PROBE_TARGETS))


def system_dns_ok(name: str = DNS_PROBE_NAME, timeout: float = 3.0) -> bool:
    pool = ThreadPoolExecutor(1)
    try:
        return bool(pool.submit(socket.getaddrinfo, name, 80).result(timeout=timeout))
    except (FutureTimeout, OSError):
        return False
    finally:
        pool.shutdown(wait=False)


def _http_status(url: str, timeout: float) -> int | None:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as c:
            return c.get(url).status_code
    except Exception:
        return None


def http_check(timeout: float = 3.0) -> str:
    """'ok' (got 204), 'redirect' (something answered instead), or 'fail'.

    Runs under a hard deadline: httpx's timeout doesn't cover the DNS lookup, which
    can hang for minutes when the link is dead.
    """
    pool = ThreadPoolExecutor(len(HTTP_204_URLS))
    futures = [pool.submit(_http_status, url, timeout) for url in HTTP_204_URLS]
    saw_other = False
    try:
        for f in as_completed(futures, timeout=timeout + 1.0):
            code = f.result()
            if code == 204:
                return "ok"
            saw_other = saw_other or code is not None
    except FutureTimeout:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return "redirect" if saw_other else "fail"


def _root_ns_query() -> tuple[int, bytes]:
    qid = secrets.randbelow(0xFFFF)
    # header: id, flags=RD, 1 question; question: root name, type NS, class IN
    return qid, struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + b"\x00" + struct.pack(">HH", 2, 1)


def dns_server_answers(server: str, timeout: float = 2.0, tries: int = 2, port: int = 53) -> bool:
    """True if `server` sends back any DNS reply, whatever the answer.

    Pointed at the carrier's own resolver this proves packets cross the cellular
    bearer and return, without depending on the public internet or a VPN. It asks
    for the root zone, so no website is looked up.
    """
    for _ in range(tries):
        qid, packet = _root_ns_query()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(packet, (server, port))
                data, _ = s.recvfrom(512)
            if len(data) >= 2 and struct.unpack(">H", data[:2])[0] == qid:
                return True
        except OSError:
            continue
    return False


@dataclass(frozen=True)
class HomeCheck:
    is_home: bool
    reason: str
    gateway: str | None
    gateway_mac: str | None
    ssid: str | None


def check_home_network(s: Settings) -> HomeCheck:
    """Only act when the default gateway is ours. Never guess from open ports."""
    gw = get_default_gateway()
    ssid = get_current_ssid()
    if not gw:
        return HomeCheck(False, "no default gateway", None, None, ssid)
    mac = get_gateway_mac(gw)
    if mac is None and gw in s.home_gateways:
        tcp_reachable(gw, 80, timeout=0.5)  # refresh an expired ARP entry
        mac = get_gateway_mac(gw)
    if gw not in s.home_gateways:
        return HomeCheck(False, f"gateway {gw} is not one of {', '.join(s.home_gateways)}", gw, mac, ssid)
    if s.home_gateway_mac and mac != s.home_gateway_mac:
        return HomeCheck(False, f"gateway MAC {mac} does not match HOME_GATEWAY_MAC", gw, mac, ssid)
    if s.allowed_ssids and ssid and ssid.casefold() not in {x.casefold() for x in s.allowed_ssids}:
        return HomeCheck(False, f"Wi-Fi '{ssid}' is not in ALLOWED_SSIDS", gw, mac, ssid)
    return HomeCheck(True, "gateway matches", gw, mac, ssid)
