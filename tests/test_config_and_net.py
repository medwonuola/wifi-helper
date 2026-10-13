import socket
from typing import Any
import struct
import threading
import pytest
from wifi_helper import net
from wifi_helper.config import Settings, host_of, normalize_mac, parse_list
from wifi_helper.modem import network_type_label


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("192.168.3.1,192.168.8.1", ("192.168.3.1", "192.168.8.1")),
        ('["192.168.3.1", "192.168.8.1"]', ("192.168.3.1", "192.168.8.1")),
        ('"Home WiFi,Home WiFi 5G"', ("Home WiFi", "Home WiFi 5G")),
        ("", ()),
        (None, ()),
    ],
)
def test_parse_list(raw, expected):
    assert parse_list(raw) == expected


def test_normalize_mac_pads_macos_arp_output():
    assert normalize_mac("2c:7:6:fc:b:15") == "2c:07:06:fc:0b:15"
    assert normalize_mac("2C-07-06-FC-0B-15") == "2c:07:06:fc:0b:15"
    assert normalize_mac("garbage") is None


def test_host_of():
    assert host_of("http://192.168.8.1") == "192.168.8.1"
    assert host_of("192.168.8.1:8080") == "192.168.8.1"
    assert host_of("") == ""


def make_settings(**kw) -> Settings:
    base: dict[str, Any] = dict(
        modem_url="http://192.168.8.1", modem_user="admin", modem_password="", router_url="", router_user="admin",
        router_password="", allowed_gateways=(), allowed_ssids=(), home_gateway_mac=None, check_interval=15,
        offline_checks_before_fix=2, reboot_cooldown=900,
    )
    base.update(kw)
    return Settings(**base)


def test_home_gateways_default_to_device_hosts():
    s = make_settings(router_url="http://192.168.3.1")
    assert s.home_gateways == ("192.168.3.1", "192.168.8.1")


@pytest.fixture
def fake_net(monkeypatch):
    def apply(gw, mac, ssid=None):
        monkeypatch.setattr(net, "get_default_gateway", lambda: gw)
        monkeypatch.setattr(net, "get_gateway_mac", lambda ip: mac)
        monkeypatch.setattr(net, "get_current_ssid", lambda: ssid)
    return apply


def test_home_check_rejects_lookalike_gateway(fake_net):
    s = make_settings(home_gateway_mac="aa:bb:cc:dd:ee:ff")
    fake_net("192.168.8.1", "11:22:33:44:55:66")
    assert not net.check_home_network(s).is_home
    fake_net("192.168.8.1", "aa:bb:cc:dd:ee:ff")
    assert net.check_home_network(s).is_home


def test_home_check_ssid_is_exact_not_substring(fake_net):
    s = make_settings(allowed_ssids=("Home",))
    fake_net("192.168.8.1", None, "Home Office Guest")
    assert not net.check_home_network(s).is_home
    fake_net("192.168.8.1", None, None)  # macOS hides SSIDs without location access
    assert net.check_home_network(s).is_home


def test_home_check_other_gateway(fake_net):
    fake_net("10.0.0.1", None)
    assert not net.check_home_network(make_settings()).is_home


def test_dns_probe_accepts_any_reply_and_rejects_silence():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    def answer_refused():
        data, addr = srv.recvfrom(512)
        qid = struct.unpack(">H", data[:2])[0]
        srv.sendto(struct.pack(">HHHHHH", qid, 0x8105, 1, 0, 0, 0), addr)  # REFUSED is still a reply

    t = threading.Thread(target=answer_refused)
    t.start()
    try:
        assert net.dns_server_answers("127.0.0.1", timeout=1.0, tries=1, port=port)
        assert not net.dns_server_answers("127.0.0.1", timeout=0.3, tries=1, port=port)
    finally:
        t.join()
        srv.close()

def test_vpn_interface_detection():
    assert net.is_vpn_interface("utun1")
    assert net.is_vpn_interface("wg0")
    assert not net.is_vpn_interface("en0")
    assert not net.is_vpn_interface(None)


def test_network_type_label_does_not_guess():
    assert network_type_label("101") == "4G LTE"
    assert network_type_label("9999") == "type 9999"
    assert network_type_label(None, "19") == "type 19"

