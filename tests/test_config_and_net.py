import socket
import threading
import dns.message
import dns.rcode
import pytest
from wifi_helper import net
from wifi_helper.config import Settings, host_of, is_ours, normalize_mac
from wifi_helper.modem import AuthError, LoginBackoff, MonitoringStatus, network_type_label
from wifi_helper.router import scram_proof


def make_settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_lists_are_comma_separated():
    s = make_settings(allowed_gateways="192.168.3.1, 192.168.8.1", allowed_ssids="Home WiFi,Home WiFi 5G,")
    assert s.allowed_gateways == ("192.168.3.1", "192.168.8.1")
    assert s.allowed_ssids == ("Home WiFi", "Home WiFi 5G")


def test_settings_reject_unsafe_intervals():
    with pytest.raises(ValueError):
        make_settings(reboot_cooldown=10)


def test_normalize_mac_pads_macos_arp_output():
    assert normalize_mac("2c:7:6:fc:b:15") == "2c:07:06:fc:0b:15"
    assert normalize_mac("2C-07-06-FC-0B-15") == "2c:07:06:fc:0b:15"
    assert normalize_mac("garbage") is None
    assert make_settings(home_gateway_mac="2C:7:6:FC:B:15").home_gateway_mac == ("2c:07:06:fc:0b:15",)
    with pytest.raises(ValueError):
        make_settings(home_gateway_mac="aa:bb:cc:dd:ee:ff,garbage")


def test_host_of():
    assert host_of("http://192.168.8.1") == "192.168.8.1"
    assert host_of("http://192.168.8.1:8080/") == "192.168.8.1"
    assert host_of("") == ""


def test_home_gateways_default_to_device_hosts():
    s = make_settings(router_url="http://192.168.3.1/")
    assert s.router_url == "http://192.168.3.1"
    assert s.home_gateways == ("192.168.3.1", "192.168.8.1")
    assert s.separate_router
    assert not make_settings(router_url="http://192.168.8.1").separate_router


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


def test_home_check_accepts_any_pinned_mac(fake_net):
    s = make_settings(home_gateway_mac="aa:bb:cc:dd:ee:ff, 11:22:33:44:55:66")
    fake_net("192.168.8.1", "11:22:33:44:55:66")
    assert net.check_home_network(s).is_home


def test_only_env_files_with_modem_settings_are_used(tmp_path):
    other, ours = tmp_path / "other.env", tmp_path / ".env"
    other.write_text("API_TOKEN=x\n")
    ours.write_text("# comment\nMODEM_URL=http://192.168.8.1\n")
    assert not is_ours(other)
    assert is_ours(ours)
    assert not is_ours(tmp_path / "missing")


def test_home_check_ssid_is_exact_not_substring(fake_net):
    s = make_settings(allowed_ssids="Home")
    fake_net("192.168.8.1", None, "Home Office Guest")
    assert not net.check_home_network(s).is_home
    fake_net("192.168.8.1", None, None)
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
        reply = dns.message.make_response(dns.message.from_wire(data))
        reply.set_rcode(dns.rcode.REFUSED)
        srv.sendto(reply.to_wire(), addr)

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


def test_monitoring_status_from_api():
    m = MonitoringStatus.from_api({
        "ConnectionStatus": "901", "SignalIcon": "4", "CurrentNetworkTypeEx": "101",
        "PrimaryDns": "10.1.1.1", "SecondaryDns": None, "SimStatus": "1",
    })
    assert m.connected and m.signal_bars == 4 and m.network_type == "4G LTE"
    assert m.carrier_dns == ["10.1.1.1"] and m.sim_ok


def test_scram_proof_known_value():
    assert scram_proof("pässword", "00ff10ab" * 8, 100, "a,b,b") == "0f8f6c3798b549840349a8782458ec41cfc06de5fc04a912d224546ca5df753f"


def test_login_backoff_doubles_and_resets():
    b = LoginBackoff(base=300, cap=3600)
    b.check(0)
    b.failed("bad password", 0)
    with pytest.raises(AuthError):
        b.check(299)
    b.check(300)
    b.failed("bad password", 300)
    assert b.until == 900
    for _ in range(10):
        b.failed("bad password", 1000)
    assert b.until == 4600
    b.succeeded()
    b.check(1000)
