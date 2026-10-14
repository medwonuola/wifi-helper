from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import time
from huawei_lte_api.Client import Client
from huawei_lte_api.Connection import Connection
from huawei_lte_api.api.Sms import Message
from huawei_lte_api.enums.device import ControlModeEnum
from huawei_lte_api.exceptions import (
    LoginErrorInvalidCredentialsException,
    LoginErrorUsernamePasswordModifyException,
    LoginErrorUsernamePasswordOverrunException,
    ResponseErrorException,
    ResponseErrorLoginRequiredException,
)
import requests
from .config import Settings

CONNECTION_STATUS = {"900": "connecting", "901": "connected", "902": "disconnected", "903": "disconnecting"}

NETWORK_TYPE_EX = {
    "0": "no service",
    "1": "2G GSM",
    "2": "2G GPRS",
    "3": "2G EDGE",
    "41": "3G WCDMA",
    "42": "3G HSDPA",
    "43": "3G HSUPA",
    "44": "3G HSPA",
    "45": "3G HSPA+",
    "46": "3G DC-HSPA+",
    "101": "4G LTE",
    "1011": "4G LTE+",
    "111": "5G NR",
}

LOGIN_ERRORS = (
    LoginErrorInvalidCredentialsException,
    LoginErrorUsernamePasswordOverrunException,
    LoginErrorUsernamePasswordModifyException,
)
USSD_PENDING = 111019
SMS_PAGE_MAX = 50


class ModemError(Exception):
    pass


class AuthError(ModemError):
    pass


def network_type_label(ex: str | None, legacy: str | None = None) -> str:
    if ex:
        return NETWORK_TYPE_EX.get(ex, f"type {ex}")
    return f"type {legacy}" if legacy else "unknown"


@dataclass(frozen=True)
class MonitoringStatus:
    connection_status: str
    signal_bars: int
    network_type: str
    primary_dns: str
    secondary_dns: str
    sim_ok: bool | None

    @classmethod
    def from_api(cls, d: dict) -> "MonitoringStatus":
        sim = d.get("SimStatus")
        return cls(
            connection_status=d.get("ConnectionStatus") or "",
            signal_bars=int(d.get("SignalIcon") or 0),
            network_type=network_type_label(d.get("CurrentNetworkTypeEx"), d.get("CurrentNetworkType")),
            primary_dns=d.get("PrimaryDns") or "",
            secondary_dns=d.get("SecondaryDns") or "",
            sim_ok=None if sim is None else sim == "1",
        )

    @property
    def connected(self) -> bool:
        return self.connection_status == "901"

    @property
    def connection_label(self) -> str:
        return CONNECTION_STATUS.get(self.connection_status, f"code {self.connection_status}")

    @property
    def carrier_dns(self) -> list[str]:
        return [d for d in (self.primary_dns, self.secondary_dns) if d]


@dataclass(frozen=True)
class ModemStatus:
    monitoring: MonitoringStatus
    operator_name: str
    wan_ip: str
    nr_active: bool
    download_bps: int
    upload_bps: int
    total_download_bytes: int
    total_upload_bytes: int


@contextmanager
def session(s: Settings, login: bool = True, timeout: float = 15.0) -> Iterator[Client]:
    if login and not s.modem_password:
        raise AuthError("MODEM_PASSWORD is not set")
    user, password = (s.modem_user, s.modem_password) if login else (None, None)
    try:
        with Connection(s.modem_url, user, password, timeout=timeout) as conn:
            yield Client(conn)
    except LOGIN_ERRORS as e:
        raise AuthError(str(e)) from e
    except (ResponseErrorException, requests.RequestException) as e:
        raise ModemError(str(e) or e.__class__.__name__) from e


def monitoring_status(s: Settings, timeout: float = 6.0) -> MonitoringStatus:
    with session(s, login=False, timeout=timeout) as c:
        try:
            return MonitoringStatus.from_api(c.monitoring.status())
        except ResponseErrorLoginRequiredException:
            pass
    with session(s, timeout=timeout) as c:
        return MonitoringStatus.from_api(c.monitoring.status())


def status(c: Client) -> ModemStatus:
    traffic = c.monitoring.traffic_statistics()
    plmn = c.net.current_plmn()
    try:
        nr_active = bool(c.device.signal().get("nrrsrp"))
    except ResponseErrorException:
        nr_active = False
    return ModemStatus(
        monitoring=MonitoringStatus.from_api(c.monitoring.status()),
        operator_name=plmn.get("FullName") or plmn.get("ShortName") or "unknown",
        wan_ip=c.device.information().get("WanIPAddress") or "",
        nr_active=nr_active,
        download_bps=int(traffic.get("CurrentDownloadRate") or 0) * 8,
        upload_bps=int(traffic.get("CurrentUploadRate") or 0) * 8,
        total_download_bytes=int(traffic.get("TotalDownload") or 0),
        total_upload_bytes=int(traffic.get("TotalUpload") or 0),
    )


def sms_counts(c: Client) -> tuple[int, int, int]:
    d = c.sms.sms_count()
    return int(d["LocalUnread"]), int(d["LocalInbox"]), int(d["LocalMax"])


def sms_list(c: Client, page: int, count: int) -> list[Message]:
    return [Message.from_dict(m) for m in c.sms.get_sms_list(page, read_count=count)["Messages"]["Message"]]


def sms_all(c: Client) -> list[Message]:
    return list(c.sms.get_messages(read_count=SMS_PAGE_MAX))


def ussd(c: Client, code: str, timeout: float) -> str:
    c.ussd.send(code)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1.0)
        try:
            return (c.ussd.get().get("content") or "").strip()
        except ResponseErrorException as e:
            if e.code != USSD_PENDING:
                raise
    return "USSD timed out waiting for the network."


def cycle_data(c: Client) -> bool:
    c.dial_up.set_mobile_dataswitch(0)
    time.sleep(2.0)
    return c.dial_up.set_mobile_dataswitch(1) == "OK"


def reboot(c: Client) -> bool:
    return c.device.set_control(ControlModeEnum.REBOOT) == "OK"
