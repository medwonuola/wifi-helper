from dataclasses import dataclass
from datetime import datetime
import time
import xml.etree.ElementTree as ET
import httpx
from .config import settings
from .crypto import generate_nonce, scram_calculate


@dataclass(frozen=True)
class ModemStatus:
    connection_status: str
    network_type: str
    signal_bars: int
    operator_name: str
    sim_status: str
    wan_ip: str
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


class ModemClient:
    def __init__(
        self,
        base_url: str = settings.modem_url,
        username: str = settings.modem_user,
        password: str = settings.modem_password,
        timeout: float = 35.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout)
        self.tokens: list[str] = []

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
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

    def _get_token(self) -> str | None:
        if self.tokens:
            return self.tokens.pop(0)
        try:
            r = self.client.get("/api/webserver/token")
            root = ET.fromstring(r.text)
            tok = root.findtext("token")
            if tok:
                return tok
        except Exception:
            pass
        return None

    def is_authenticated(self) -> bool:
        try:
            r = self.get("/api/sms/sms-count")
            return "<LocalInbox>" in r.text
        except Exception:
            return False

    def login(self) -> bool:
        if self.is_authenticated():
            return True
        try:
            r = self.client.get("/api/webserver/SesTokInfo")
            root = ET.fromstring(r.text)
            token = root.findtext("TokInfo") or ""
            nonce = generate_nonce()

            chal_xml = f"""<?xml version="1.0" encoding="UTF-8"?><request><username>{self.username}</username><firstnonce>{nonce}</firstnonce><mode>1</mode></request>"""
            headers = {
                "__RequestVerificationToken": token,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            }
            r_chal = self.client.post("/api/user/challenge_login", content=chal_xml, headers=headers)
            if "125003" in r_chal.text:
                time.sleep(1.0)
                r_chal = self.client.post("/api/user/challenge_login", content=chal_xml, headers=headers)
            if "<error>" in r_chal.text:
                return False

            self._add_tokens_from_response(r_chal)
            root_chal = ET.fromstring(r_chal.text)
            salt = root_chal.findtext("salt")
            iters_str = root_chal.findtext("iterations")
            sn = root_chal.findtext("servernonce")
            if not salt or not iters_str or not sn:
                return False

            proof, _ = scram_calculate(self.password, salt, int(iters_str), f"{nonce},{sn},{sn}")
            auth_xml = f"""<?xml version="1.0" encoding="UTF-8"?><request><clientproof>{proof}</clientproof><finalnonce>{sn}</finalnonce></request>"""
            headers["__RequestVerificationToken"] = self._get_token() or token

            r_auth = self.client.post("/api/user/authentication_login", content=auth_xml, headers=headers)
            self._add_tokens_from_response(r_auth)
            return "<response>" in r_auth.text and "<error>" not in r_auth.text
        except Exception:
            return False

    def logout(self):
        try:
            self.post("/api/user/logout", "<request><Logout>1</Logout></request>")
        except Exception:
            pass

    def get(self, endpoint: str) -> httpx.Response:
        headers = {
            "_ResponseSource": "Broswer",
            "X-Requested-With": "XMLHttpRequest",
        }
        tok = self._get_token()
        if tok:
            headers["__RequestVerificationToken"] = tok
        r = self.client.get(endpoint, headers=headers)
        self._add_tokens_from_response(r)
        return r

    def post(self, endpoint: str, xml_content: str) -> httpx.Response:
        tok = self._get_token()
        headers = {
            "__RequestVerificationToken": tok or "",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }
        r = self.client.post(endpoint, content=xml_content, headers=headers)
        self._add_tokens_from_response(r)
        return r

    def status(self) -> ModemStatus:
        r_mon = self.get("/api/monitoring/status")
        root_mon = ET.fromstring(r_mon.text)

        r_plmn = self.get("/api/net/current-plmn")
        root_plmn = ET.fromstring(r_plmn.text)

        r_traf = self.get("/api/monitoring/traffic-statistics")
        root_traf = ET.fromstring(r_traf.text)

        r_dev = self.get("/api/device/information")
        root_dev = ET.fromstring(r_dev.text)

        return ModemStatus(
            connection_status=root_mon.findtext("ConnectionStatus", "Unknown"),
            network_type="5G NSA" if root_mon.findtext("CurrentNetworkTypeEx") == "101" else root_mon.findtext("CurrentNetworkType", "Unknown"),
            signal_bars=int(root_mon.findtext("SignalIcon", "0")),
            operator_name=root_plmn.findtext("FullName") or root_plmn.findtext("ShortName", "Unknown"),
            sim_status="OK" if root_mon.findtext("SimStatus") == "1" else "Error",
            wan_ip=root_dev.findtext("WanIPAddress", ""),
            current_download_rate=int(root_traf.findtext("CurrentDownloadRate", "0")),
            current_upload_rate=int(root_traf.findtext("CurrentUploadRate", "0")),
            total_download_bytes=int(root_traf.findtext("TotalDownload", "0")),
            total_upload_bytes=int(root_traf.findtext("TotalUpload", "0")),
        )

    def sms_counts(self) -> tuple[int, int, int]:
        r = self.get("/api/sms/sms-count")
        root = ET.fromstring(r.text)
        unread = int(root.findtext("LocalUnread", "0"))
        inbox = int(root.findtext("LocalInbox", "0"))
        max_msgs = int(root.findtext("LocalMax", "500"))
        return unread, inbox, max_msgs

    def sms_list(self, page: int = 1, count: int = 20) -> list[SMSMessage]:
        req_xml = f"""<?xml version="1.0" encoding="UTF-8"?><request><pageindex>{page}</pageindex><readcount>{count}</readcount></request>"""
        r = self.post("/api/sms/sms-list-contact", req_xml)
        root = ET.fromstring(r.text)

        messages: list[SMSMessage] = []
        for msg in root.findall(".//message"):
            messages.append(
                SMSMessage(
                    phone=msg.findtext("phone", ""),
                    date=msg.findtext("date", ""),
                    content=msg.findtext("content", ""),
                    is_unread=msg.findtext("smstat", "") == "0",
                )
            )
        return messages

    def sms_send(self, phone: str, content: str) -> bool:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        req_xml = f"""<?xml version="1.0" encoding="UTF-8"?><request><Index>-1</Index><Phones><Phone>{phone}</Phone></Phones><Sca></Sca><Content>{content}</Content><Length>{len(content)}</Length><Reserved>1</Reserved><Date>{now_str}</Date></request>"""
        r = self.post("/api/sms/send-sms", req_xml)
        return "<response>OK</response>" in r.text

    def ussd_send(self, code: str, timeout_seconds: int = 20) -> str:
        self.get("/api/ussd/release")
        send_xml = f"""<?xml version="1.0" encoding="UTF-8"?><request><content>{code}</content><codeType>CodeType</codeType><timeout></timeout></request>"""
        r_send = self.post("/api/ussd/send", send_xml)
        if "<response>OK</response>" not in r_send.text:
            self.get("/api/ussd/release")
            return f"Failed to send USSD code: {r_send.text}"

        start_time = time.time()
        result = "USSD timed out waiting for network response."
        while time.time() - start_time < timeout_seconds:
            time.sleep(1.0)
            r_get = self.get("/api/ussd/get")
            if "<content>" in r_get.text:
                root = ET.fromstring(r_get.text)
                content = root.findtext("content", "").strip()
                if content:
                    result = content
                    break
            elif "111019" not in r_get.text:
                result = f"USSD error response: {r_get.text}"
                break

        self.get("/api/ussd/release")
        return result

    def cycle_data(self) -> bool:
        r_off = self.post("/api/dialup/mobile-dataswitch", """<?xml version="1.0" encoding="UTF-8"?><request><dataswitch>0</dataswitch></request>""")
        time.sleep(2.0)
        r_on = self.post("/api/dialup/mobile-dataswitch", """<?xml version="1.0" encoding="UTF-8"?><request><dataswitch>1</dataswitch></request>""")
        return "<response>OK</response>" in r_on.text

    def reboot(self) -> bool:
        r = self.post("/api/device/control", """<?xml version="1.0" encoding="UTF-8"?><request><Control>1</Control></request>""")
        return "<response>OK</response>" in r.text
