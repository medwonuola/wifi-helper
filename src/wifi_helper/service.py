from dataclasses import dataclass
from pathlib import Path
import plistlib
import subprocess
import sys
from .config import ENV_FILE, state_dir

LABEL = "io.github.wifi-helper"
UNIT = "wifi-helper.service"


@dataclass(frozen=True)
class ServiceResult:
    ok: bool
    message: str


def log_path() -> Path:
    return state_dir() / "daemon.log"


def _program() -> list[str]:
    return [sys.executable, "-m", "wifi_helper", "daemon"]


def _env() -> dict[str, str]:
    return {"PYTHONUNBUFFERED": "1"} | ({"WIFI_HELPER_ENV": str(ENV_FILE)} if ENV_FILE else {})


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT


def _run(*cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _result(path: Path, res: subprocess.CompletedProcess) -> ServiceResult:
    return ServiceResult(res.returncode == 0, str(path) if res.returncode == 0 else res.stderr.strip())


def _unsupported() -> ServiceResult:
    return ServiceResult(False, f"background service isn't supported on {sys.platform}; run `wifi daemon` instead")


def install() -> ServiceResult:
    log = str(log_path())
    log_path().parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        path = _plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps({
            "Label": LABEL,
            "ProgramArguments": _program(),
            "EnvironmentVariables": _env(),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }))
        _run("launchctl", "unload", str(path))
        return _result(path, _run("launchctl", "load", str(path)))
    if sys.platform.startswith("linux"):
        path = _unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        env = "".join(f'Environment="{k}={v}"\n' for k, v in _env().items())
        path.write_text(
            "[Unit]\nDescription=wifi-helper connection watchdog\nAfter=network-online.target\n\n"
            f"[Service]\nExecStart={' '.join(_program())}\n{env}Restart=always\nRestartSec=10\n"
            f"StandardOutput=append:{log}\nStandardError=append:{log}\n\n[Install]\nWantedBy=default.target\n"
        )
        _run("systemctl", "--user", "daemon-reload")
        return _result(path, _run("systemctl", "--user", "enable", "--now", UNIT))
    return _unsupported()


def uninstall() -> ServiceResult:
    if sys.platform == "darwin":
        path = _plist_path()
        if path.exists():
            _run("launchctl", "unload", str(path))
    elif sys.platform.startswith("linux"):
        path = _unit_path()
        if path.exists():
            _run("systemctl", "--user", "disable", "--now", UNIT)
    else:
        return _unsupported()
    if not path.exists():
        return ServiceResult(False, "service is not installed")
    path.unlink()
    return ServiceResult(True, f"removed {path}")


def status() -> ServiceResult:
    if sys.platform == "darwin":
        line = next((ln for ln in _run("launchctl", "list").stdout.splitlines() if LABEL in ln), None)
        return ServiceResult(bool(line), line or "not running")
    if sys.platform.startswith("linux"):
        state = _run("systemctl", "--user", "is-active", UNIT).stdout.strip()
        return ServiceResult(state == "active", state or "not running")
    return _unsupported()
