from dataclasses import dataclass
import hashlib
import hmac
import re
import secrets
from typing import Any
import requests
from .config import Settings


class RouterError(Exception):
    pass


def scram_proof(password: str, salt_hex: str, iterations: int, auth_msg: str) -> str:
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), iterations)
    client_key = hmac.digest(b"Client Key", salted, "sha256")
    signature = hmac.digest(auth_msg.encode(), hashlib.sha256(client_key).digest(), "sha256")
    return bytes(a ^ b for a, b in zip(client_key, signature)).hex()


@dataclass(frozen=True)
class RouterStatus:
    device_name: str
    firmware: str
    wan_ip: str
    wan_gateway: str
    wan_status: str
    active_hosts: int


class RouterClient:
    def __init__(self, s: Settings, timeout: float = 15.0):
        self.url, self.username, self.password, self.timeout = s.router_url, s.router_user, s.router_password, timeout
        self.http = requests.Session()
        self.http.headers.update({"_ResponseFormat": "JSON", "X-Requested-With": "XMLHttpRequest"})
        self.csrf: dict[str, str] = {}

    def __enter__(self) -> "RouterClient":
        try:
            self._login()
        except BaseException:
            self.http.close()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self.http.close()

    def _request(self, path: str, data: dict | None = None) -> requests.Response:
        try:
            if data is None:
                return self.http.get(self.url + path, timeout=self.timeout)
            return self.http.post(self.url + path, json={"csrf": self.csrf, "data": data}, timeout=self.timeout)
        except requests.RequestException as e:
            raise RouterError(f"router: {e}") from e

    def _call(self, path: str, data: dict | None = None) -> Any:
        try:
            body = self._request(path, data).json()
        except requests.JSONDecodeError as e:
            raise RouterError("router sent a non-JSON reply (still booting?)") from e
        if isinstance(body, dict) and "csrf_token" in body:
            self.csrf = {"csrf_param": body["csrf_param"], "csrf_token": body["csrf_token"]}
        return body

    def _login(self) -> None:
        if not self.password:
            raise RouterError("ROUTER_PASSWORD is not set")
        page = self._request("/html/index.html").text
        self.csrf = dict(re.findall(r'name="(csrf_param|csrf_token)"\s+content="([^"]+)"', page))
        if len(self.csrf) != 2:
            raise RouterError("unrecognised router login page (a different router model?)")
        nonce = secrets.token_hex(32)
        challenge = self._call("/api/system/user_login_nonce", {"username": self.username, "firstnonce": nonce})
        salt, iterations, server_nonce = (challenge.get(k) for k in ("salt", "iterations", "servernonce"))
        if not (salt and iterations and server_nonce):
            raise RouterError("unexpected router login challenge")
        proof = scram_proof(self.password, salt, iterations, f"{nonce},{server_nonce},{server_nonce}")
        if self._call("/api/system/user_login_proof", {"clientproof": proof, "finalnonce": server_nonce}).get("err") != 0:
            raise RouterError("router login failed (wrong password?)")

    def status(self) -> RouterStatus:
        dev = self._call("/api/system/deviceinfo")
        wans = self._call("/api/ntwk/wan")
        hosts = self._call("/api/system/HostInfo")
        wan = wans[0] if wans else {}
        return RouterStatus(
            device_name=dev.get("custinfo", {}).get("CustZHFriendlyName", "Huawei router"),
            firmware=dev.get("HarmonyOSVersion", "unknown"),
            wan_ip=wan.get("IPv4Addr", ""),
            wan_gateway=wan.get("IPv4Gateway", ""),
            wan_status=wan.get("ConnectionStatus", "unknown"),
            active_hosts=sum(1 for h in hosts if h.get("Active")),
        )

    def reboot(self) -> bool:
        res = self._call("/api/system/delayreboot", {})
        return 0 in (res.get("errcode"), res.get("err"))
