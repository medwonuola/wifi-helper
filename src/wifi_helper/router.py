from dataclasses import dataclass
import re
import httpx
from .config import settings
from .crypto import generate_nonce, scram_calculate
from .modem import AuthError


@dataclass(frozen=True)
class RouterStatus:
    device_name: str
    harmony_version: str
    wan_ip: str
    wan_gateway: str
    wan_status: str
    active_hosts_count: int


class RouterClient:
    def __init__(
        self,
        base_url: str = settings.router_url,
        username: str = settings.router_user,
        password: str = settings.router_password,
        timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout)
        self.csrf_param: str = ""
        self.csrf_token: str = ""
        self.logged_in = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.client.close()

    def _update_csrf(self, data: dict):
        if "csrf_param" in data and "csrf_token" in data:
            self.csrf_param = data["csrf_param"]
            self.csrf_token = data["csrf_token"]

    def login(self) -> None:
        """Log in or raise AuthError."""
        if self.logged_in:
            return
        if not self.password:
            raise AuthError("ROUTER_PASSWORD is not set")
        if not self._login():
            raise AuthError("router login failed (wrong password, or a different router model)")
        self.logged_in = True

    def _login(self) -> bool:
        try:
            r_index = self.client.get("/html/index.html")
            m_param = re.search(r'name="csrf_param"\s+content="([^"]+)"', r_index.text)
            m_token = re.search(r'name="csrf_token"\s+content="([^"]+)"', r_index.text)
            if not m_param or not m_token:
                return False

            self.csrf_param = m_param.group(1)
            self.csrf_token = m_token.group(1)
            nonce = generate_nonce()

            payload = {
                "csrf": {"csrf_param": self.csrf_param, "csrf_token": self.csrf_token},
                "data": {"username": self.username, "firstnonce": nonce},
            }
            headers = {
                "_ResponseFormat": "JSON",
                "Content-Type": "application/json; charset=utf-8",
                "X-Requested-With": "XMLHttpRequest",
            }
            r_nonce = self.client.post("/api/system/user_login_nonce", json=payload, headers=headers)
            nonce_data = r_nonce.json()
            self._update_csrf(nonce_data)

            salt = nonce_data.get("salt")
            iters = nonce_data.get("iterations")
            sn = nonce_data.get("servernonce")
            if not salt or not iters or not sn:
                return False

            proof, _ = scram_calculate(self.password, salt, iters, f"{nonce},{sn},{sn}")
            proof_payload = {
                "csrf": {"csrf_param": self.csrf_param, "csrf_token": self.csrf_token},
                "data": {"clientproof": proof, "finalnonce": sn},
            }
            r_proof = self.client.post("/api/system/user_login_proof", json=proof_payload, headers=headers)
            proof_data = r_proof.json()
            self._update_csrf(proof_data)
            return proof_data.get("err") == 0
        except Exception:
            return False

    def get(self, endpoint: str) -> httpx.Response:
        headers = {
            "_ResponseFormat": "JSON",
            "Content-Type": "application/json; charset=utf-8",
            "X-Requested-With": "XMLHttpRequest",
        }
        r = self.client.get(endpoint, headers=headers)
        try:
            self._update_csrf(r.json())
        except Exception:
            pass
        return r

    def post(self, endpoint: str, data: dict) -> httpx.Response:
        payload = {
            "csrf": {"csrf_param": self.csrf_param, "csrf_token": self.csrf_token},
            "data": data,
        }
        headers = {
            "_ResponseFormat": "JSON",
            "Content-Type": "application/json; charset=utf-8",
            "X-Requested-With": "XMLHttpRequest",
        }
        r = self.client.post(endpoint, json=payload, headers=headers)
        try:
            self._update_csrf(r.json())
        except Exception:
            pass
        return r

    def status(self) -> RouterStatus:
        self.login()
        r_dev = self.get("/api/system/deviceinfo").json()
        r_wan = self.get("/api/ntwk/wan").json()
        r_hosts = self.get("/api/system/HostInfo").json()

        active_hosts = len([h for h in r_hosts if isinstance(h, dict) and h.get("Active")])
        wan_entry = r_wan[0] if isinstance(r_wan, list) and r_wan else {}

        return RouterStatus(
            device_name=r_dev.get("custinfo", {}).get("CustZHFriendlyName", "Huawei router"),
            harmony_version=r_dev.get("HarmonyOSVersion", "Unknown"),
            wan_ip=wan_entry.get("IPv4Addr", ""),
            wan_gateway=wan_entry.get("IPv4Gateway", ""),
            wan_status=wan_entry.get("ConnectionStatus", "Unknown"),
            active_hosts_count=active_hosts,
        )

    def reboot(self) -> bool:
        self.login()
        r = self.post("/api/system/delayreboot", {})
        data = r.json()
        return data.get("errcode", -1) == 0 or data.get("err", -1) == 0
