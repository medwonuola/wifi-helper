from dataclasses import dataclass, field
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


@dataclass(frozen=True)
class Settings:
    modem_url: str = os.getenv("MODEM_URL", "http://192.168.118.1")
    modem_user: str = os.getenv("MODEM_USER", "admin")
    modem_password: str = os.getenv("MODEM_PASSWORD", "")

    router_url: str = os.getenv("ROUTER_URL", "http://192.168.3.1")
    router_user: str = os.getenv("ROUTER_USER", "admin")
    router_password: str = os.getenv("ROUTER_PASSWORD", "")

    allowed_gateways: tuple[str, ...] = tuple(
        x.strip() for x in os.getenv("ALLOWED_GATEWAYS", "192.168.3.1,192.168.118.1").split(",") if x.strip()
    )
    allowed_ssids: tuple[str, ...] = tuple(
        x.strip().strip('"').strip("'") for x in os.getenv("ALLOWED_SSIDS", "Free Internet,Free Internet 3.0").split(",") if x.strip()
    )
    check_interval: int = int(os.getenv("CHECK_INTERVAL", "15"))
    reboot_cooldown: int = int(os.getenv("REBOOT_COOLDOWN", "300"))


settings = Settings()
