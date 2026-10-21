from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout, as_completed
from dataclasses import dataclass
import re
import shutil
import socket
import subprocess
import sys
import dns.exception
import dns.message
import dns.query
import requests
from .config import Settings, normalize_mac

VPN_IFACE_PREFIXES = ("utun", "tun", "tap", "wg", "ppp", "ipsec", "cloudflarewarp", "tailscale", "zt")
HTTP_204_URLS = ("http://connectivitycheck.gstatic.com/generate_204", "http://cp.cloudflare.com/generate_204")
IP_PROBE_TARGETS = (("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 443))
DNS_PROBE_NAME = "connectivitycheck.gstatic.com"


def _run(cmd: list[str], stdin: str | None = None) -> str:
    exe = shutil.which(cmd[0])
    if exe is None:
        return ""
    try:
        return subprocess.run([exe, *cmd[1:]], input=stdin, capture_output=True, text=True, timeout=2, close_fds=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def get_default_gateway() -> str | None:
    if sys.platform == "darwin":
        m = re.search(r"^default\s+(\d+\.\d+\.\d+\.\d+)", _run(["netstat", "-rn", "-f", "inet"]), re.M)
    else:
        m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", _run(["ip", "-4", "route", "show", "default"]))
    return m.group(1) if m else None


def _scutil(key: str) -> str:
    return _run(["scutil"], stdin=f"show {key}\n")


def get_gateway_mac(ip: str) -> str | None:
    if sys.platform != "darwin":
        m = re.search(r"lladdr ([0-9a-fA-F:]+)", _run(["ip", "neigh", "show", ip]))
        return normalize_mac(m.group(1)) if m else None
    svc = re.search(r"PrimaryService : (\S+)", _scutil("State:/Network/Global/IPv4"))
    info = _scutil(f"State:/Network/Service/{svc.group(1)}/IPv4") if svc else ""
    m_ip = re.search(r"ARPResolvedIPAddress : (\S+)", info)
    m_mac = re.search(r"ARPResolvedHardwareAddress : (\S+)", info)
    if m_ip and m_mac and m_ip.group(1) == ip:
        return normalize_mac(m_mac.group(1))
    m = re.search(r" at ([0-9a-fA-F:]+) ", _run(["arp", "-n", ip]))
    return normalize_mac(m.group(1)) if m else None


def get_current_ssid() -> str | None:
    if sys.platform == "darwin":
        out = _run(["networksetup", "-getairportnetwork", "en0"])
        if "Current Wi-Fi Network:" in out:
            return out.split(":", 1)[1].strip() or None
        m = re.search(r"\n\s*SSID\s*:\s*(.+)", _run(["ipconfig", "getsummary", "en0"]))
        return m.group(1).strip() if m and "<redacted>" not in m.group(1) else None
    if out := _run(["iwgetid", "-r"]).strip():
        return out
    active = (line[4:] for line in _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]).splitlines() if line.startswith("yes:"))
    return next(active, None) or None


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
        return requests.get(url, timeout=timeout, allow_redirects=False).status_code
    except requests.RequestException:
        return None


def http_check(timeout: float = 3.0) -> str:
    pool = ThreadPoolExecutor(len(HTTP_204_URLS))
    futures = [pool.submit(_http_status, url, timeout) for url in HTTP_204_URLS]
    saw_other = False
    try:
        for f in as_completed(futures, timeout=timeout + 1.0):
            if (code := f.result()) == 204:
                return "ok"
            saw_other |= code is not None
    except FutureTimeout:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return "redirect" if saw_other else "fail"


def dns_server_answers(server: str, timeout: float = 2.0, tries: int = 2, port: int = 53) -> bool:
    for _ in range(tries):
        try:
            dns.query.udp(dns.message.make_query(".", "NS"), server, timeout=timeout, port=port)
            return True
        except (dns.exception.DNSException, OSError):
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
    gw = get_default_gateway()
    ssid = get_current_ssid()
    if not gw:
        return HomeCheck(False, "no default gateway", None, None, ssid)
    if gw not in s.home_gateways:
        return HomeCheck(False, f"gateway {gw} is not one of {', '.join(s.home_gateways)}", gw, get_gateway_mac(gw), ssid)
    mac = get_gateway_mac(gw)
    if mac is None:
        tcp_reachable(gw, 80, timeout=0.5)
        mac = get_gateway_mac(gw)
    if s.home_gateway_mac and mac not in s.home_gateway_mac:
        return HomeCheck(False, f"gateway MAC {mac} does not match HOME_GATEWAY_MAC", gw, mac, ssid)
    if s.allowed_ssids and ssid and ssid.casefold() not in {x.casefold() for x in s.allowed_ssids}:
        return HomeCheck(False, f"Wi-Fi '{ssid}' is not in ALLOWED_SSIDS", gw, mac, ssid)
    return HomeCheck(True, "gateway matches", gw, mac, ssid)
