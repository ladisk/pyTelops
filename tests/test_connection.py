from unittest.mock import MagicMock

import numpy as np

from pyTelops.connection import (
    ConnectionReport,
    TrialResult,
    _is_usb_adapter,
    _link_local_warning,
    _rank_trials,
    tune_connection,
)
from pyTelops.errors import DownloadStats


class TestLinkLocalWarning:
    """The link-local/VPN warning must not false-fire on a normal direct link."""

    def test_single_link_local_no_warning(self):
        # A single link-local NIC IS the normal direct camera connection.
        assert _link_local_warning([("Ethernet 7", "169.254.27.140")]) is None

    def test_no_link_local_no_warning(self):
        assert _link_local_warning([("Ethernet", "192.168.1.5")]) is None

    def test_multiple_link_local_warns(self):
        w = _link_local_warning([("Ethernet 7", "169.254.27.140"), ("Tailscale", "169.254.83.107")])
        assert w is not None
        assert "local_ip" in w

    def test_vpn_named_adapter_warns(self):
        # Tailscale up on a routed (non-link-local) address alongside the camera.
        w = _link_local_warning(
            [("Ethernet 7", "169.254.27.140"), ("Tailscale", "100.102.181.121")]
        )
        assert w is not None


class TestIsUsbAdapter:
    """USB detection must use the adapter description, not just the NIC name."""

    def test_asix_description_detected(self):
        assert _is_usb_adapter("Ethernet 7", "ASIX USB to Gigabit Ethernet") is True

    def test_plain_ethernet_not_usb(self):
        assert _is_usb_adapter("Ethernet", "Intel(R) Ethernet Connection I219-V") is False

    def test_usb_in_name_detected(self):
        assert _is_usb_adapter("USB Ethernet", "") is True

    def test_realtek_usb_dongle_detected(self):
        assert _is_usb_adapter("Ethernet 3", "Realtek USB GbE Family Controller") is True


def test_rank_prefers_zero_drops_over_speed():
    fast_lossy = TrialResult(
        packet_size=8000,
        bitrate_mbps=1000,
        socket_buffer=0,
        throughput_mbps=900.0,
        pct_incomplete=0.5,
    )
    slow_clean = TrialResult(
        packet_size=1500,
        bitrate_mbps=500,
        socket_buffer=0,
        throughput_mbps=400.0,
        pct_incomplete=0.0,
    )
    ranked = _rank_trials([fast_lossy, slow_clean])
    assert ranked[0] is slow_clean


def test_rank_breaks_ties_by_throughput():
    a = TrialResult(
        packet_size=1500,
        bitrate_mbps=500,
        socket_buffer=0,
        throughput_mbps=400.0,
        pct_incomplete=0.0,
    )
    b = TrialResult(
        packet_size=1500,
        bitrate_mbps=1000,
        socket_buffer=0,
        throughput_mbps=650.0,
        pct_incomplete=0.0,
    )
    ranked = _rank_trials([a, b])
    assert ranked[0] is b


def test_connection_report_recommended_is_top_rank():
    a = TrialResult(
        packet_size=1500,
        bitrate_mbps=300,
        socket_buffer=0,
        throughput_mbps=250.0,
        pct_incomplete=0.0,
    )
    b = TrialResult(
        packet_size=1500,
        bitrate_mbps=1000,
        socket_buffer=0,
        throughput_mbps=600.0,
        pct_incomplete=0.0,
    )
    report = ConnectionReport.from_trials([a, b], probe=None, warnings=[])
    assert report.recommended["bitrate_mbps"] == 1000
    assert report.recommended["packet_size"] == 1500


def _mock_cam():
    cam = MagicMock()
    cam._probe_max_packet_size.return_value = 1500
    return cam


def test_tune_connection_probe_only_skips_downloads():
    cam = _mock_cam()
    report = tune_connection(cam, probe_only=True)
    assert report.sweep == []
    cam.buffer_download.assert_not_called()
    assert report.probe is not None
    assert report.probe.max_packet_size == 1500


def test_tune_connection_sweeps_and_recommends():
    cam = _mock_cam()

    def run(**kwargs):
        bitrate = kwargs.get("bitrate_mbps", 1000)
        cam.last_download_stats = DownloadStats(
            n_frames=300,
            n_incomplete=0,
            throughput_mbps=bitrate * 0.6,
            elapsed_s=1.0,
            packet_size_used=kwargs.get("packet_size", 1500),
            bitrate_used=bitrate,
        )
        return np.ones((300, 4, 4), np.uint16)

    cam.buffer_download.side_effect = run
    report = tune_connection(
        cam,
        candidate_packet_sizes=[1500],
        candidate_bitrates=[300, 1000],
        socket_buffers=[0],
        test_frames=300,
    )
    assert report.recommended["bitrate_mbps"] == 1000
    assert len(report.sweep) == 2
