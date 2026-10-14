import os
import sys
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    return ([Path(explicit)] if explicit else []) + [Path.cwd() / ".env", config_dir() / "env"]


ENV_FILE = next((p for p in env_candidates() if p.is_file()), None)


def normalize_mac(mac: str | None) -> str | None:
    parts = (mac or "").strip().lower().replace("-", ":").split(":")
    try:
        return ":".join(f"{int(p, 16):02x}" for p in parts) if len(parts) == 6 else None
    except ValueError:
        return None


def host_of(url: str) -> str:
    return urlparse(url).hostname or ""


CommaList = Annotated[tuple[str, ...], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore", frozen=True)

    modem_url: str = "http://192.168.8.1"
    modem_user: str = "admin"
    modem_password: str = ""
    router_url: str = ""
    router_user: str = "admin"
    router_password: str = ""
    allowed_gateways: CommaList = ()
    allowed_ssids: CommaList = ()
    home_gateway_mac: str | None = None
    check_interval: Annotated[int, Field(ge=5)] = 15
    offline_checks_before_fix: Annotated[int, Field(ge=1)] = 2
    reboot_cooldown: Annotated[int, Field(ge=60)] = 900

    @field_validator("allowed_gateways", "allowed_ssids", mode="before")
    @classmethod
    def _split(cls, v: object) -> object:
        return tuple(x.strip() for x in v.split(",") if x.strip()) if isinstance(v, str) else v

    @field_validator("home_gateway_mac")
    @classmethod
    def _mac(cls, v: str | None) -> str | None:
        return normalize_mac(v)

    @field_validator("modem_url", "router_url")
    @classmethod
    def _url(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def modem_host(self) -> str:
        return host_of(self.modem_url)

    @property
    def router_host(self) -> str:
        return host_of(self.router_url)

    @property
    def separate_router(self) -> bool:
        return bool(self.router_host) and self.router_host != self.modem_host

    @property
    def home_gateways(self) -> tuple[str, ...]:
        return self.allowed_gateways or tuple(h for h in (self.router_host, self.modem_host) if h)


settings = Settings()
