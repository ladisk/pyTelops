from unittest.mock import MagicMock

import numpy as np

from pyTelops.connection import ConnectionReport, TrialResult, _rank_trials, tune_connection
from pyTelops.errors import DownloadStats


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
