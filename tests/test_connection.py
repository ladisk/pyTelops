from pyTelops.connection import ConnectionReport, TrialResult, _rank_trials


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
