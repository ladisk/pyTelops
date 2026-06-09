import logging
from unittest.mock import MagicMock

import numpy as np
import pytest

from pyTelops import registers as reg
from pyTelops.camera import (
    Camera,
    _group_contiguous,
    _missing_positions,
    _pace_bitrate,
    _resolve_packet_size,
)
from pyTelops.errors import DownloadStats, FrameIntegrityError


class TestMissingPositions:
    def test_covers_never_arrived_and_partial(self):
        assert _missing_positions(5, {0, 2, 4}) == [1, 3]

    def test_none_missing(self):
        assert _missing_positions(3, {0, 1, 2}) == []

    def test_all_missing(self):
        assert _missing_positions(3, set()) == [0, 1, 2]


class TestGroupContiguous:
    def test_runs(self):
        assert _group_contiguous([1, 2, 3, 5, 8, 9]) == [(1, 3), (5, 5), (8, 9)]

    def test_empty(self):
        assert _group_contiguous([]) == []

    def test_unsorted_input(self):
        assert _group_contiguous([9, 1, 8, 2]) == [(1, 2), (8, 9)]


class TestPaceBitrate:
    def test_round0_is_base(self):
        assert _pace_bitrate(0, 1000) == 1000.0

    def test_halves_each_round(self):
        assert _pace_bitrate(1, 1000) == 500.0
        assert _pace_bitrate(2, 1000) == 250.0

    def test_clamped_to_floor(self):
        assert _pace_bitrate(10, 1000, floor=100.0) == 100.0


def test_download_stats_defaults():
    s = DownloadStats(n_frames=10)
    assert s.n_frames == 10
    assert s.n_incomplete == 0
    assert s.incomplete_frame_ids == []
    assert s.per_frame_missing == {}
    assert s.resend_requested == 0


def test_frame_integrity_error_carries_stats():
    s = DownloadStats(n_frames=10, n_incomplete=2, incomplete_frame_ids=[3, 7])
    err = FrameIntegrityError("boom", stats=s)
    assert err.stats is s
    assert "boom" in str(err)


def test_resolve_packet_size_standard_passes_through():
    assert _resolve_packet_size(requested=1500, probe_max=1500) == (1500, None)


def test_resolve_packet_size_jumbo_supported():
    size, warn = _resolve_packet_size(requested=8000, probe_max=9000)
    assert size == 8000
    assert warn is None


def test_resolve_packet_size_jumbo_unsupported_falls_back():
    size, warn = _resolve_packet_size(requested=9000, probe_max=1500)
    assert size == 1500
    assert warn is not None
    assert "1500" in warn


def test_resolve_packet_size_probe_unknown_keeps_request():
    # probe_max None means the probe could not run; do not second-guess.
    assert _resolve_packet_size(requested=4000, probe_max=None) == (4000, None)


def _fake_cam_for_download():
    cam = Camera()
    cam._connected = True
    cam._streaming = False
    cam._acquiring = False
    cam._local_ip = "169.254.1.1"
    cam._gvcp = MagicMock()
    cam._gvcp.read_reg.return_value = 0
    cam._gvcp.read_float.return_value = 1000.0
    cam._gvcp._control_lost = False
    cam._gvsp = MagicMock()
    cam._gvsp._resend_stats = {"requested": 0, "recovered": 0, "failed": 0}
    cam._gvsp.port = 3957
    cam._gvsp._sock.getsockname.return_value = ("169.254.1.1", 3957)
    cam.start_stream = MagicMock()
    cam.stop_stream = MagicMock()
    return cam


def _complete_range(frame_id, count, **kwargs):
    """A ``_download_range`` mock where every position arrives complete.

    Each frame's pixel value is set to its global frame id (``frame_id + off``)
    so the assembled array's ordering can be asserted.
    """
    return {
        off: (np.full((4, 4), frame_id + off, dtype=np.uint16), {"missing_packets": 0})
        for off in range(count)
    }


def test_buffer_download_clean_returns_all_in_order():
    cam = _fake_cam_for_download()
    cam._download_range = MagicMock(side_effect=_complete_range)
    out = cam.buffer_download(n_frames=3, convert=False, strip_header=False, verbose=False)
    assert out.shape[0] == 3
    assert [int(out[i, 0, 0]) for i in range(3)] == [0, 1, 2]
    assert cam.last_download_stats.n_incomplete == 0


def test_buffer_download_recovers_never_arrived_in_order():
    cam = _fake_cam_for_download()
    calls = []

    def dr(frame_id, count, **kw):
        calls.append((frame_id, count, kw["bitrate_mbps"]))
        if frame_id == 0 and count == 5:
            # First pass: positions 1 and 3 never arrive.
            return {
                off: (np.full((4, 4), off, np.uint16), {"missing_packets": 0}) for off in (0, 2, 4)
            }
        return _complete_range(frame_id, count)

    cam._download_range = MagicMock(side_effect=dr)
    out = cam.buffer_download(n_frames=5, convert=False, strip_header=False, verbose=False)
    assert out.shape[0] == 5
    # Never-arrived frames recovered AND placed at the right positions.
    assert [int(out[i, 0, 0]) for i in range(5)] == [0, 1, 2, 3, 4]
    assert cam.last_download_stats.n_incomplete == 0
    assert cam.last_download_stats.recovered_by_retry == 2
    assert calls[0][2] == 1000.0  # first pass at base bitrate
    assert calls[1][2] == 500.0  # recovery round paced lower


def test_buffer_download_raises_when_unrecoverable():
    cam = _fake_cam_for_download()

    def dr(frame_id, count, **kw):
        if frame_id == 0 and count == 4:
            return {off: (np.ones((4, 4), np.uint16), {"missing_packets": 0}) for off in (0, 1, 3)}
        return {}  # position 2 can never be recovered

    cam._download_range = MagicMock(side_effect=dr)
    with pytest.raises(FrameIntegrityError):
        cam.buffer_download(n_frames=4, retries=2, convert=False, strip_header=False, verbose=False)
    assert cam.last_download_stats.n_incomplete == 1
    assert cam.last_download_stats.incomplete_frame_ids == [2]  # start_frame 0 + position 2


def test_buffer_download_tolerates_when_allowed():
    cam = _fake_cam_for_download()

    def dr(frame_id, count, **kw):
        if frame_id == 0 and count == 4:
            return {off: (np.ones((4, 4), np.uint16), {"missing_packets": 0}) for off in (0, 1, 3)}
        return {}

    cam._download_range = MagicMock(side_effect=dr)
    out = cam.buffer_download(
        n_frames=4,
        retries=1,
        max_dropped_frames=5,
        convert=False,
        strip_header=False,
        verbose=False,
    )
    assert out is not None
    assert out.shape[0] == 3  # the complete frames, in order
    assert cam.last_download_stats.n_incomplete == 1
    assert cam.last_download_stats.resend_requested == 0


def test_buffer_download_passes_resend_flag():
    for want in (True, False):
        cam = _fake_cam_for_download()
        seen = {}

        def dr(frame_id, count, _seen=seen, **kw):
            _seen["resend"] = kw["resend"]
            return _complete_range(frame_id, count)

        cam._download_range = MagicMock(side_effect=dr)
        cam.buffer_download(
            n_frames=2, resend=want, convert=False, strip_header=False, verbose=False
        )
        assert seen["resend"] is want


def test_buffer_download_default_disables_resend():
    # Resends ON during bulk download caused congestion collapse on hardware;
    # the default must keep them OFF (resend=True stays available opt-in).
    cam = _fake_cam_for_download()
    seen = {}

    def dr(frame_id, count, **kw):
        seen["resend"] = kw["resend"]
        return _complete_range(frame_id, count)

    cam._download_range = MagicMock(side_effect=dr)
    cam.buffer_download(n_frames=1, convert=False, strip_header=False, verbose=False)
    assert seen["resend"] is False
    cam._gvsp.reset_resend_stats.assert_called_once()


def test_buffer_download_resets_stats_on_empty_buffer():
    cam = _fake_cam_for_download()
    cam._download_range = MagicMock(side_effect=_complete_range)
    cam.buffer_download(n_frames=1, convert=False, strip_header=False, verbose=False)
    assert cam.last_download_stats is not None
    # Empty buffer: recorded size reads 0, so n_frames resolves to 0 -> None.
    cam._gvcp.read_reg.return_value = 0
    out = cam.buffer_download(n_frames=0, convert=False, strip_header=False, verbose=False)
    assert out is None
    assert cam.last_download_stats is None


def test_download_diagnostics_reports_incomplete(caplog):
    data = np.zeros((3, 4, 4), dtype=np.float32)
    stats = DownloadStats(n_frames=3, n_incomplete=1, incomplete_frame_ids=[2])
    with caplog.at_level(logging.WARNING, logger="pyTelops.camera"):
        Camera._download_diagnostics(data, expected=4, stats=stats)
    msgs = " ".join(r.message for r in caplog.records)
    assert (
        "incomplete" in msgs.lower() or "missing" in msgs.lower() or "never arrived" in msgs.lower()
    )


def test_download_diagnostics_ok_path(caplog):
    data = np.ones((4, 4, 4), dtype=np.float32)
    stats = DownloadStats(n_frames=4, n_incomplete=0)
    with caplog.at_level(logging.INFO, logger="pyTelops.camera"):
        Camera._download_diagnostics(data, expected=4, stats=stats)
    msgs = " ".join(r.message for r in caplog.records)
    assert "OK" in msgs or "ok" in msgs


def test_buffer_download_converges_over_multiple_rounds():
    cam = _fake_cam_for_download()
    state = {"straggler_attempts": 0}

    def dr(frame_id, count, **kw):
        if frame_id == 0 and count == 3:
            # First pass: only position 1 is missing.
            return {off: (np.ones((4, 4), np.uint16), {"missing_packets": 0}) for off in (0, 2)}
        if frame_id == 1:  # the straggler range; succeeds only on the 2nd try
            state["straggler_attempts"] += 1
            if state["straggler_attempts"] >= 2:
                return {0: (np.ones((4, 4), np.uint16), {"missing_packets": 0})}
            return {}
        return _complete_range(frame_id, count)

    cam._download_range = MagicMock(side_effect=dr)
    out = cam.buffer_download(
        n_frames=3, retries=4, convert=False, strip_header=False, verbose=False
    )
    assert out.shape[0] == 3
    assert cam.last_download_stats.n_incomplete == 0
    assert state["straggler_attempts"] >= 2  # took more than one recovery round


def _probe_cam():
    cam = _fake_cam_for_download()

    def reg_reads(r):
        if r == reg.REG_MEMORY_BUFFER_SEQ_RECORDED_SIZE:
            return 10
        if r == reg.REG_MEMORY_BUFFER_SEQ_FIRST_FRAME_ID:
            return 1
        return 0

    cam._gvcp.read_reg.side_effect = reg_reads
    return cam


def test_probe_returns_largest_size_that_delivers_complete_frames():
    cam = _probe_cam()

    def dr(frame_id, count, *, packet_size, **kw):
        miss = 0 if packet_size <= 8000 else 5  # 9000 does not deliver here
        return {
            off: (np.ones((4, 4), np.uint16), {"missing_packets": miss}) for off in range(count)
        }

    cam._download_range = MagicMock(side_effect=dr)
    assert cam._probe_max_packet_size(9000) == 8000


def test_probe_returns_requested_when_jumbo_delivers():
    cam = _probe_cam()
    cam._download_range = MagicMock(
        side_effect=lambda frame_id, count, *, packet_size, **kw: {
            off: (np.ones((4, 4), np.uint16), {"missing_packets": 0}) for off in range(count)
        }
    )
    assert cam._probe_max_packet_size(9000) == 9000


def test_probe_returns_1500_when_buffer_empty():
    cam = _fake_cam_for_download()  # read_reg returns 0 -> recorded size 0
    cam._download_range = MagicMock()
    assert cam._probe_max_packet_size(9000) == 1500
    cam._download_range.assert_not_called()


def test_buffer_download_falls_back_when_jumbo_unsupported(caplog):
    cam = _fake_cam_for_download()
    cam._probe_max_packet_size = MagicMock(return_value=1500)
    cam._download_range = MagicMock(side_effect=_complete_range)
    with caplog.at_level(logging.WARNING, logger="pyTelops.camera"):
        cam.buffer_download(
            n_frames=2, packet_size=9000, convert=False, strip_header=False, verbose=False
        )
    assert cam.last_download_stats.packet_size_used == 1500
    assert any("1500" in r.message for r in caplog.records)


def test_public_exports():
    import pyTelops

    assert hasattr(pyTelops, "FrameIntegrityError")
    assert hasattr(pyTelops, "DownloadStats")
    assert hasattr(pyTelops, "tune_connection")
    assert hasattr(pyTelops, "ConnectionReport")
