from unittest.mock import patch

from pyTelops.camera import discover


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
