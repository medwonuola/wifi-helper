"""Install the watchdog as a per-user background service (launchd on macOS, systemd on Linux)."""

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from .config import ENV_FILE, state_dir

LABEL = "io.github.wifi-helper"


@dataclass(frozen=True)
class ServiceResult:
    ok: bool
    message: str


def _program() -> list[str]:
    return [sys.executable, "-m", "wifi_helper", "daemon"]


def log_path() -> Path:
    return state_dir() / "daemon.log"


def _env_lines() -> dict[str, str]:
    return {"WIFI_HELPER_ENV": str(ENV_FILE)} if ENV_FILE else {}


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "wifi-helper.service"


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def install() -> ServiceResult:
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        args = "\n".join(f"        <string>{_xml(a)}</string>" for a in _program())
        env = "".join(f"\n        <key>{k}</key><string>{_xml(v)}</string>" for k, v in _env_lines().items())
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
    <key>EnvironmentVariables</key>
    <dict>{env}
        <key>PYTHONUNBUFFERED</key><string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{_xml(str(log))}</string>
    <key>StandardErrorPath</key>
    <string>{_xml(str(log))}</string>
</dict>
</plist>
"""
        path = _plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plist)
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        res = subprocess.run(["launchctl", "load", str(path)], capture_output=True, text=True)
        return ServiceResult(res.returncode == 0, f"{path}" if res.returncode == 0 else res.stderr.strip())

    if sys.platform.startswith("linux"):
        env = "".join(f'Environment="{k}={v}"\n' for k, v in _env_lines().items())
        unit = f"""[Unit]
Description=wifi-helper connection watchdog
After=network-online.target

[Service]
ExecStart={" ".join(_program())}
{env}Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=10
StandardOutput=append:{log}
StandardError=append:{log}

[Install]
WantedBy=default.target
"""
        path = _unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(unit)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        res = subprocess.run(["systemctl", "--user", "enable", "--now", "wifi-helper.service"], capture_output=True, text=True)
        return ServiceResult(res.returncode == 0, f"{path}" if res.returncode == 0 else res.stderr.strip())

    return ServiceResult(False, f"background service install isn't supported on {sys.platform}; run `wifi daemon` instead")


def uninstall() -> ServiceResult:
    if sys.platform == "darwin":
        path = _plist_path()
        if not path.exists():
            return ServiceResult(False, "service is not installed")
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        path.unlink()
        return ServiceResult(True, f"removed {path}")
    if sys.platform.startswith("linux"):
        path = _unit_path()
        if not path.exists():
            return ServiceResult(False, "service is not installed")
        subprocess.run(["systemctl", "--user", "disable", "--now", "wifi-helper.service"], capture_output=True)
        path.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        return ServiceResult(True, f"removed {path}")
    return ServiceResult(False, "not supported on this platform")


def status() -> ServiceResult:
    if sys.platform == "darwin":
        res = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
        line = next((ln for ln in res.stdout.splitlines() if LABEL in ln), None)
        return ServiceResult(bool(line), line or "not running")
    if sys.platform.startswith("linux"):
        res = subprocess.run(["systemctl", "--user", "is-active", "wifi-helper.service"], capture_output=True, text=True)
        return ServiceResult(res.stdout.strip() == "active", res.stdout.strip() or "not running")
    return ServiceResult(False, "not supported on this platform")
