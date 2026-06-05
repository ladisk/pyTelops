import types
from unittest.mock import patch

from pyTelops.camera import _is_reachable, discover


def test_discover_calls_gvcp_discover_all_interfaces():
    fake = [
        {
            "ip": "192.168.0.5",
            "manufacturer": "Telops Inc.",
            "model": "M3k",
            "mac": "aa:bb:cc:dd:ee:ff",
        }
    ]
    with patch("pyTelops.camera.GVCPClient.discover", return_value=fake) as m:
        out = discover()
    m.assert_called_once_with("", 2.0)
    assert out and out[0]["ip"] == "192.168.0.5"


def test_discover_filters_non_telops():
    fake = [
        {"ip": "1.1.1.1", "manufacturer": "Telops Inc.", "model": "M3k"},
        {"ip": "2.2.2.2", "manufacturer": "FLIR", "model": "X"},
    ]
    with patch("pyTelops.camera.GVCPClient.discover", return_value=fake):
        out = discover()
    assert [c["ip"] for c in out] == ["1.1.1.1"]


def _snic(addr, netmask):
    import socket

    return types.SimpleNamespace(family=socket.AF_INET, address=addr, netmask=netmask)


def test_is_reachable_same_subnet():
    subnets = [("192.168.0.0", "255.255.255.0")]
    assert _is_reachable("192.168.0.42", subnets) is True


def test_is_reachable_different_subnet():
    subnets = [("192.168.0.0", "255.255.255.0")]
    assert _is_reachable("169.254.2.43", subnets) is False


def test_discover_attaches_reachable_flag():
    fake = [{"ip": "169.254.2.43", "manufacturer": "Telops Inc.", "model": "M3k"}]
    stats = {"eth0": types.SimpleNamespace(isup=True)}
    addrs = {"eth0": [_snic("192.168.0.10", "255.255.255.0")]}
    with (
        patch("pyTelops.camera.GVCPClient.discover", return_value=fake),
        patch("psutil.net_if_addrs", return_value=addrs),
        patch("psutil.net_if_stats", return_value=stats),
    ):
        out = discover()
    assert out[0]["reachable"] is False


def test_connect_raises_on_unreachable(monkeypatch):
    import pytest

    from pyTelops.camera import Camera

    fake = [
        {"ip": "169.254.2.43", "manufacturer": "Telops Inc.", "model": "M3k", "reachable": False}
    ]
    monkeypatch.setattr("pyTelops.camera.discover", lambda *a, **k: fake)
    cam = Camera()
    with pytest.raises(RuntimeError, match="not on any host"):
        cam.connect()
