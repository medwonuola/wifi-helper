"""Client for the HiLink web API used by Huawei 4G/5G CPEs, routers and dongles."""

import base64
from dataclasses import dataclass
from datetime import datetime
import hashlib
import re
import time
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET
import httpx
from .config import settings
from .crypto import generate_nonce, scram_calculate

CONNECTION_STATUS = {
    "900": "connecting",
    "901": "connected",
    "902": "disconnected",
    "903": "disconnecting",
}

# CurrentNetworkTypeEx. Unknown values are shown raw rather than guessed.
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

AUTH_ERRORS = {
    "108001": "wrong username",
    "108002": "wrong password",
    "108006": "wrong username or password",
    "108007": "login rejected: wrong credentials or too many failed attempts",
    "108010": "too many failed logins; the device is refusing logins for a while",
}


class ModemError(Exception):
    pass


class AuthError(ModemError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def _error_code(text: str) -> str | None:
    m = re.search(r"<error>\s*<code>(\d+)</code>", text)
    return m.group(1) if m else None


def network_type_label(ex: str | None, legacy: str | None = None) -> str:
    if ex and ex in NETWORK_TYPE_EX:
        return NETWORK_TYPE_EX[ex]
    if ex:
        return f"type {ex}"
    return f"type {legacy}" if legacy else "unknown"


@dataclass(frozen=True)
class MonitoringStatus:
    connection_status: str
    signal_bars: int
    network_type: str
    primary_dns: str
    secondary_dns: str
    sim_ok: bool | None

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
    current_download_rate: int
    current_upload_rate: int
    total_download_bytes: int
    total_upload_bytes: int


@dataclass(frozen=True)
class SMSMessage:
    phone: str
    date: str
    content: str
    is_unread: bool


def _int(text: str | None) -> int:
    try:
        return int(text or 0)
    except ValueError:
        return 0


class ModemClient:
    def __init__(
        self,
        base_url: str = settings.modem_url,
        username: str = settings.modem_user,
        password: str = settings.modem_password,
        timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout)
        self.tokens: list[str] = []
        self.logged_in = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.logged_in:
            self.logout()
        self.client.close()

    def _add_tokens_from_response(self, resp: httpx.Response):
        for header in (
            "__requestverificationtoken",
            "__RequestVerificationToken",
            "__requestverificationtokenone",
            "__RequestVerificationTokenone",
        ):
            val = resp.headers.get(header)
            if val:
                for tok in val.split("#"):
                    clean = tok.strip()
                    if clean and clean not in self.tokens:
                        self.tokens.append(clean)

    def _new_session(self) -> str:
        """Fetch a session cookie and token. Most requests fail with 125002 without one."""
        try:
            r = self.client.get("/api/webserver/SesTokInfo")
        except (httpx.RemoteProtocolError, httpx.ReadError):
            r = self.client.get("/api/webserver/SesTokInfo")
        return ET.fromstring(r.text).findtext("TokInfo") or ""

    def _get_token(self) -> str | None:
        if self.tokens:
            return self.tokens.pop(0)
        if not self.client.cookies:
            try:
                return self._new_session() or None
            except Exception:
                return None
        try:
            r = self.client.get("/api/webserver/token")
            tok = ET.fromstring(r.text).findtext("token")
            if tok:
                return tok
        except Exception:
            pass
        try:
            return self._new_session() or None
        except Exception:
            return None

    def _parse(self, r: httpx.Response) -> ET.Element:
        code = _error_code(r.text)
        if code == "100003":
            raise AuthError("this request needs a login", code)
        if code:
            raise ModemError(f"modem returned error {code}")
        return ET.fromstring(r.text)

    def get(self, endpoint: str) -> httpx.Response:
        headers = {"_ResponseSource": "Broswer", "X-Requested-With": "XMLHttpRequest"}
        tok = self._get_token()
        if tok:
            headers["__RequestVerificationToken"] = tok
        try:
            r = self.client.get(endpoint, headers=headers)
        except (httpx.RemoteProtocolError, httpx.ReadError):
            # The modem's web server sometimes drops a reused connection; GETs are safe to retry.
            r = self.client.get(endpoint, headers=headers)
        self._add_tokens_from_response(r)
        return r

    def post(self, endpoint: str, xml_content: str) -> httpx.Response:
        headers = {
            "__RequestVerificationToken": self._get_token() or "",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }
        r = self.client.post(endpoint, content=xml_content, headers=headers)
        self._add_tokens_from_response(r)
        return r

    def get_xml(self, endpoint: str) -> ET.Element:
        r = self.get(endpoint)
        if _error_code(r.text) in ("125001", "125002", "125003"):
            self.tokens.clear()
            self.tokens.append(self._new_session())
            r = self.get(endpoint)
        return self._parse(r)

    def login(self) -> None:
        """Log in or raise AuthError. Tries SCRAM first, then the older SHA-256 scheme."""
        if self.logged_in:
            return
        if not self.password:
            raise AuthError("MODEM_PASSWORD is not set")
        token = self._new_session()
        headers = {
            "__RequestVerificationToken": token,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }
        nonce = generate_nonce()
        chal_xml = (
            '<?xml version="1.0" encoding="UTF-8"?><request>'
            f"<username>{escape(self.username)}</username><firstnonce>{nonce}</firstnonce><mode>1</mode></request>"
        )
        r_chal = self.client.post("/api/user/challenge_login", content=chal_xml, headers=headers)
        if _error_code(r_chal.text) == "125003":
            time.sleep(1.0)
            r_chal = self.client.post("/api/user/challenge_login", content=chal_xml, headers=headers)
        code = _error_code(r_chal.text)
        if code in ("100002", "100004", "100006"):
            return self._legacy_login(token)
        if code:
            raise AuthError(AUTH_ERRORS.get(code, f"login challenge failed (error {code})"), code)

        self._add_tokens_from_response(r_chal)
        root = ET.fromstring(r_chal.text)
        salt, iters, sn = root.findtext("salt"), root.findtext("iterations"), root.findtext("servernonce")
        if not salt or not iters or not sn:
            raise AuthError("unexpected login challenge response")

        proof, _ = scram_calculate(self.password, salt, int(iters), f"{nonce},{sn},{sn}")
        auth_xml = (
            '<?xml version="1.0" encoding="UTF-8"?><request>'
            f"<clientproof>{proof}</clientproof><finalnonce>{sn}</finalnonce></request>"
        )
        headers["__RequestVerificationToken"] = self._get_token() or token
        r_auth = self.client.post("/api/user/authentication_login", content=auth_xml, headers=headers)
        self._add_tokens_from_response(r_auth)
        code = _error_code(r_auth.text)
        if code or "<response>" not in r_auth.text:
            raise AuthError(AUTH_ERRORS.get(code or "", f"login failed (error {code})"), code)
        self.logged_in = True

    def _legacy_login(self, token: str) -> None:
        # Older HiLink firmware (B310/B315/B525/E3372...): password_type 4.
        pw_hash = base64.b64encode(hashlib.sha256(self.password.encode()).hexdigest().encode())
        digest = hashlib.sha256(self.username.encode() + pw_hash + token.encode()).hexdigest()
        encoded = base64.b64encode(digest.encode()).decode()
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?><request>'
            f"<Username>{escape(self.username)}</Username><Password>{encoded}</Password>"
            "<password_type>4</password_type></request>"
        )
        r = self.client.post(
            "/api/user/login",
            content=xml,
            headers={"__RequestVerificationToken": token, "X-Requested-With": "XMLHttpRequest"},
        )
        self._add_tokens_from_response(r)
        code = _error_code(r.text)
        if code or "<response>OK</response>" not in r.text:
            raise AuthError(AUTH_ERRORS.get(code or "", f"login failed (error {code})"), code)
        self.logged_in = True

    def logout(self):
        try:
            self.post("/api/user/logout", "<request><Logout>1</Logout></request>")
        except Exception:
            pass
        self.logged_in = False

    def monitoring_status(self) -> MonitoringStatus:
        """Works without a login on most firmware; logs in only if the modem insists."""
        try:
            root = self.get_xml("/api/monitoring/status")
        except AuthError:
            self.login()
            root = self.get_xml("/api/monitoring/status")
        sim = root.findtext("SimStatus")
        return MonitoringStatus(
            connection_status=root.findtext("ConnectionStatus", "") or "",
            signal_bars=_int(root.findtext("SignalIcon")),
            network_type=network_type_label(root.findtext("CurrentNetworkTypeEx"), root.findtext("CurrentNetworkType")),
            primary_dns=root.findtext("PrimaryDns", "") or "",
            secondary_dns=root.findtext("SecondaryDns", "") or "",
            sim_ok=None if sim is None else sim == "1",
        )

    def status(self) -> ModemStatus:
        self.login()
        mon = self.monitoring_status()
        plmn = self.get_xml("/api/net/current-plmn")
        traf = self.get_xml("/api/monitoring/traffic-statistics")
        dev = self.get_xml("/api/device/information")
        try:
            sig = self.get_xml("/api/device/signal")
            nr_active = bool((sig.findtext("nrrsrp") or "").strip())
        except ModemError:
            nr_active = False
        return ModemStatus(
            monitoring=mon,
            operator_name=plmn.findtext("FullName") or plmn.findtext("ShortName") or "unknown",
            wan_ip=dev.findtext("WanIPAddress", "") or "",
            nr_active=nr_active,
            current_download_rate=_int(traf.findtext("CurrentDownloadRate")),
            current_upload_rate=_int(traf.findtext("CurrentUploadRate")),
            total_download_bytes=_int(traf.findtext("TotalDownload")),
            total_upload_bytes=_int(traf.findtext("TotalUpload")),
        )

    def sms_counts(self) -> tuple[int, int, int]:
        self.login()
        root = self.get_xml("/api/sms/sms-count")
        return _int(root.findtext("LocalUnread")), _int(root.findtext("LocalInbox")), _int(root.findtext("LocalMax")) or 500

    def sms_list(self, page: int = 1, count: int = 20) -> list[SMSMessage]:
        self.login()
        req_xml = (
            '<?xml version="1.0" encoding="UTF-8"?><request>'
            f"<pageindex>{int(page)}</pageindex><readcount>{int(count)}</readcount></request>"
        )
        root = self._parse(self.post("/api/sms/sms-list-contact", req_xml))
        return [
            SMSMessage(
                phone=msg.findtext("phone", ""),
                date=msg.findtext("date", ""),
                content=msg.findtext("content", ""),
                is_unread=msg.findtext("smstat", "") == "0",
            )
            for msg in root.findall(".//message")
        ]

    def sms_send(self, phone: str, content: str) -> bool:
        self.login()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        req_xml = (
            '<?xml version="1.0" encoding="UTF-8"?><request><Index>-1</Index>'
            f"<Phones><Phone>{escape(phone)}</Phone></Phones><Sca></Sca><Content>{escape(content)}</Content>"
            f"<Length>{len(content)}</Length><Reserved>1</Reserved><Date>{now_str}</Date></request>"
        )
        return "<response>OK</response>" in self.post("/api/sms/send-sms", req_xml).text

    def ussd_send(self, code: str, timeout_seconds: int = 20) -> str:
        self.login()
        self.get("/api/ussd/release")
        send_xml = (
            '<?xml version="1.0" encoding="UTF-8"?><request>'
            f"<content>{escape(code)}</content><codeType>CodeType</codeType><timeout></timeout></request>"
        )
        r_send = self.post("/api/ussd/send", send_xml)
        if "<response>OK</response>" not in r_send.text:
            self.get("/api/ussd/release")
            raise ModemError(f"USSD send failed (error {_error_code(r_send.text)})")

        start = time.time()
        result = "USSD timed out waiting for the network."
        while time.time() - start < timeout_seconds:
            time.sleep(1.0)
            r_get = self.get("/api/ussd/get")
            if "<content>" in r_get.text:
                content = (ET.fromstring(r_get.text).findtext("content") or "").strip()
                if content:
                    result = content
                    break
            elif _error_code(r_get.text) != "111019":
                result = f"USSD error {_error_code(r_get.text)}"
                break
        self.get("/api/ussd/release")
        return result

    def cycle_data(self, off_seconds: float = 2.0) -> bool:
        """Turn mobile data off and on. Tears down the data session and dials a fresh one."""
        self.login()
        xml = '<?xml version="1.0" encoding="UTF-8"?><request><dataswitch>{}</dataswitch></request>'
        self.post("/api/dialup/mobile-dataswitch", xml.format(0))
        time.sleep(off_seconds)
        return "<response>OK</response>" in self.post("/api/dialup/mobile-dataswitch", xml.format(1)).text

    def reboot(self) -> bool:
        self.login()
        r = self.post("/api/device/control", '<?xml version="1.0" encoding="UTF-8"?><request><Control>1</Control></request>')
        return "<response>OK</response>" in r.text
