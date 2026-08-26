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
