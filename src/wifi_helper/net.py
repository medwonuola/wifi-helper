import re
import socket
import subprocess
import httpx
from .config import settings


def get_default_gateway() -> str | None:
    try:
        proc = subprocess.run(
            ["netstat", "-rn", "-f", "inet"],
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "default" and not parts[1].startswith("link#"):
                return parts[1]
    except Exception:
        pass
    return None


def get_current_ssid() -> str | None:
    for cmd in [
        ["networksetup", "-getairportnetwork", "en0"],
        ["ipconfig", "getsummary", "en0"],
    ]:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            if "Current Wi-Fi Network:" in proc.stdout:
                return proc.stdout.split(":", 1)[1].strip()
            m = re.search(r"SSID\s*:\s*(.+)", proc.stdout)
            if m:
                val = m.group(1).strip()
                if val and "<redacted>" not in val:
                    return val
        except Exception:
            continue
    return None


def is_port_open(host: str, port: int = 80, timeout: float = 0.5) -> bool:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.close()
        return True
    except Exception:
        return False


def is_target_network() -> bool:
    gw = get_default_gateway()
    if gw and gw in settings.allowed_gateways:
        return True

    ssid = get_current_ssid()
    if ssid and any(target.lower() in ssid.lower() for target in settings.allowed_ssids):
        return True

    modem_host = settings.modem_url.replace("http://", "").replace("https://", "").split("/")[0].split(":")[0]
    router_host = settings.router_url.replace("http://", "").replace("https://", "").split("/")[0].split(":")[0]

    return is_port_open(router_host, 80) or is_port_open(modem_host, 80)


def has_internet(timeout: float = 2.5) -> bool:
    urls = [
        "http://connectivitycheck.gstatic.com/generate_204",
        "http://1.1.1.1",
        "http://1.0.0.1",
    ]
    for url in urls:
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.get(url, follow_redirects=False)
                if resp.status_code in (200, 204, 301, 302):
                    return True
        except Exception:
            continue
    return False
