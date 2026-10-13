from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def config_dir() -> Path:
    base = os.getenv("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "wifi-helper"


def state_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "wifi-helper"
    base = os.getenv("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "wifi-helper"


def env_candidates() -> list[Path]:
    explicit = os.getenv("WIFI_HELPER_ENV")
    paths = [Path(explicit)] if explicit else []
    paths += [Path.cwd() / ".env", REPO_ROOT / ".env", config_dir() / "env"]
    return paths


def find_env_file() -> Path | None:
    for p in env_candidates():
        if p.is_file():
            return p
    return None


ENV_FILE = find_env_file()
if ENV_FILE:
    load_dotenv(ENV_FILE)


def parse_list(raw: str | None) -> tuple[str, ...]:
    """Accept `a,b`, `"a,b"` or `["a","b"]`."""
    if not raw or not raw.strip():
        return ()
    raw = raw.strip()
    if raw.startswith("["):
        try:
            return tuple(str(x).strip() for x in json.loads(raw) if str(x).strip())
        except json.JSONDecodeError:
            raw = raw.strip("[]")
    return tuple(x.strip().strip('"').strip("'").strip() for x in raw.split(",") if x.strip().strip('"').strip("'").strip())


def normalize_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    parts = mac.strip().lower().replace("-", ":").split(":")
    if len(parts) != 6:
        return None
    try:
        return ":".join(f"{int(p, 16):02x}" for p in parts)
    except ValueError:
        return None


def host_of(url: str) -> str:
    if not url:
        return ""
    return urlparse(url if "://" in url else f"http://{url}").hostname or ""


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    modem_url: str
    modem_user: str
    modem_password: str
    router_url: str
    router_user: str
    router_password: str
    allowed_gateways: tuple[str, ...]
    allowed_ssids: tuple[str, ...]
    home_gateway_mac: str | None
    check_interval: int
    offline_checks_before_fix: int
    reboot_cooldown: int

    @property
    def modem_host(self) -> str:
        return host_of(self.modem_url)

    @property
    def router_host(self) -> str:
        return host_of(self.router_url)

    @property
    def router_configured(self) -> bool:
        return bool(self.router_host)

    @property
    def home_gateways(self) -> tuple[str, ...]:
        if self.allowed_gateways:
            return self.allowed_gateways
        return tuple(h for h in (self.router_host, self.modem_host) if h)


def load_settings() -> Settings:
    return Settings(
        modem_url=os.getenv("MODEM_URL", "http://192.168.8.1").rstrip("/"),
        modem_user=os.getenv("MODEM_USER", "admin"),
        modem_password=os.getenv("MODEM_PASSWORD", ""),
        router_url=os.getenv("ROUTER_URL", "").rstrip("/"),
        router_user=os.getenv("ROUTER_USER", "admin"),
        router_password=os.getenv("ROUTER_PASSWORD", ""),
        allowed_gateways=parse_list(os.getenv("ALLOWED_GATEWAYS")),
        allowed_ssids=parse_list(os.getenv("ALLOWED_SSIDS")),
        home_gateway_mac=normalize_mac(os.getenv("HOME_GATEWAY_MAC")),
        check_interval=max(5, _int("CHECK_INTERVAL", 15)),
        offline_checks_before_fix=max(1, _int("OFFLINE_CHECKS_BEFORE_FIX", 2)),
        reboot_cooldown=max(60, _int("REBOOT_COOLDOWN", 900)),
    )


settings = load_settings()
