import pytest
from app.vpn_manager import vpn_manager
from app.routers.live import sanitize_vpn_data, validate_vpn_payload
from fastapi import HTTPException

def test_vpn_port_allocation():
    port1 = vpn_manager._allocate_port()
    port2 = vpn_manager._allocate_port()
    assert port1 >= 10500
    assert port2 > port1

def test_sanitize_vpn_data():
    content = " [Interface] \n PrivateKey = secret \n Address = 10.0.0.2/32 \n "
    name, sanitized = sanitize_vpn_data("wireguard", "wg0", content)
    assert name == "wg0"
    assert " [Interface] " not in sanitized
    assert "[Interface]" in sanitized

    # Mode none clears name and returns sanitized tuple
    name_none, _ = sanitize_vpn_data("none", "wg0", content)
    assert name_none == ""

def test_validate_vpn_payload():
    with pytest.raises(HTTPException) as exc:
        validate_vpn_payload("wireguard", "")
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        validate_vpn_payload("wireguard", "   ")
    assert exc.value.status_code == 400

    # Valid payload does not raise
    validate_vpn_payload("wireguard", "[Interface]\nPrivateKey=abc")
    validate_vpn_payload("none", "")


def test_optimize_wireguard_conf_mtu_and_keepalive():
    raw_conf = """[Interface]
PrivateKey = aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=
Address = 10.0.0.2/32
DNS = 1.1.1.1
MTU = 1420

[Peer]
PublicKey = bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb=
Endpoint = 1.2.3.4:51820
AllowedIPs = 0.0.0.0/0
"""
    optimized = vpn_manager._optimize_wireguard_conf(raw_conf, 10501, 10502)
    assert "MTU = 1420" in optimized
    assert "PersistentKeepalive = 25" in optimized
    assert "[HTTP]" in optimized
    assert "BindAddress = 127.0.0.1:10501" in optimized
    assert "[Socks5]" in optimized
    assert "BindAddress = 127.0.0.1:10502" in optimized


def test_optimize_wireguard_conf_preserves_low_mtu():
    raw_conf = """[Interface]
PrivateKey = aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=
Address = 10.0.0.2/32
MTU = 1280

[Peer]
PublicKey = bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb=
Endpoint = 1.2.3.4:51820
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 15
"""
    optimized = vpn_manager._optimize_wireguard_conf(raw_conf, 10501, 10502)
    assert "MTU = 1280" in optimized
    assert "PersistentKeepalive = 15" in optimized
    assert "PersistentKeepalive = 25" not in optimized


def test_vpn_manager_protocol_selection():
    from app.vpn_manager import VPNProcess
    proc = VPNProcess("test", "wireguard", "http://127.0.0.1:10501", socks_url="socks5://127.0.0.1:10502")
    vpn_manager._global_vpn_process = proc
    vpn_manager._global_proxy_url = "http://127.0.0.1:10501"
    vpn_manager._global_socks_url = "socks5://127.0.0.1:10502"

    stream_item = {"id": "test_s", "use_vpn": True}
    assert vpn_manager.get_proxy_url_for_stream(stream_item, protocol="http") == "http://127.0.0.1:10501"
    assert vpn_manager.get_proxy_url_for_stream(stream_item, protocol="socks5") == "socks5://127.0.0.1:10502"

    # Cleanup mock
    vpn_manager._global_vpn_process = None
    vpn_manager._global_proxy_url = None
    vpn_manager._global_socks_url = None

