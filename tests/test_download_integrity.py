import logging
from unittest.mock import MagicMock

import numpy as np
import pytest

from pyTelops.camera import (
    Camera,
    _build_integrity_report,
    _plan_frame_retries,
    _resolve_packet_size,
)
from pyTelops.errors import DownloadStats, FrameIntegrityError


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


def test_build_integrity_report_all_perfect():
    info = [(i, {"block_id": 100 + i, "missing_packets": 0}) for i in range(5)]
    stats = _build_integrity_report(
        per_frame_info=info,
        resend_stats={"requested": 0, "recovered": 0, "failed": 0},
        n_requested=5,
    )
    assert stats.n_frames == 5
    assert stats.n_incomplete == 0
    assert stats.incomplete_frame_ids == []
    assert stats.per_frame_missing == {}


def test_build_integrity_report_flags_incomplete():
    info = [
        (0, {"block_id": 100, "missing_packets": 0}),
        (1, {"block_id": 101, "missing_packets": 4}),
        (2, {"block_id": 102, "missing_packets": 0}),
        (3, {"block_id": 103, "missing_packets": 1}),
    ]
    stats = _build_integrity_report(
        per_frame_info=info,
        resend_stats={"requested": 12, "recovered": 7, "failed": 5},
        n_requested=5,
    )
    assert stats.n_frames == 4
    assert sorted(stats.incomplete_frame_ids) == [101, 103]
    assert stats.per_frame_missing == {101: 4, 103: 1}
    # 4 frames arrived, 5 requested -> 1 never arrived. n_incomplete counts
    # both arrived-but-partial (2) AND never-arrived (1) = 3; incomplete_frame_ids
    # lists only the retryable arrived-partial subset.
    assert stats.n_incomplete == 3
    assert stats.resend_requested == 12
    assert stats.resend_recovered == 7
    assert stats.resend_failed == 5


def test_plan_frame_retries_basic():
    assert _plan_frame_retries([103, 101, 101], already_retried=set()) == [101, 103]


def test_plan_frame_retries_excludes_already_retried():
    assert _plan_frame_retries([101, 103, 105], already_retried={101}) == [103, 105]


def test_plan_frame_retries_empty():
    assert _plan_frame_retries([], already_retried=set()) == []


def test_plan_frame_retries_caps_batch():
    ids = list(range(200, 260))
    out = _plan_frame_retries(ids, already_retried=set(), max_batch=16)
    assert out == list(range(200, 216))


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


def _frame(missing, bid):
    return (np.ones((4, 4), dtype=np.uint16), {"block_id": bid, "missing_packets": missing})


def test_buffer_download_raises_on_incomplete_by_default():
    cam = _fake_cam_for_download()
    cam._gvsp.get_frame_with_info.side_effect = [_frame(0, 0), _frame(3, 1), None]
    with pytest.raises(FrameIntegrityError):
        cam.buffer_download(n_frames=2, convert=False, strip_header=False, verbose=False, retries=0)
    assert cam.last_download_stats is not None
    assert cam.last_download_stats.n_incomplete >= 1


def test_buffer_download_tolerates_when_allowed():
    cam = _fake_cam_for_download()
    cam._gvsp.get_frame_with_info.side_effect = [_frame(0, 0), _frame(3, 1), None]
    out = cam.buffer_download(
        n_frames=2,
        convert=False,
        strip_header=False,
        verbose=False,
        retries=0,
        max_dropped_frames=5,
    )
    assert out is not None
    assert out.shape[0] == 2
    assert cam.last_download_stats.resend_requested == 0


def test_buffer_download_honors_resend_flag_during_stream():
    for want in (True, False):
        cam = _fake_cam_for_download()
        captured = {}

        def grab(timeout, _cam=cam, _cap=captured):
            _cap["resend"] = _cam._gvsp.resend_enabled
            _cap["calls"] = _cap.get("calls", 0) + 1
            return _frame(0, 0) if _cap["calls"] == 1 else None

        cam._gvsp.get_frame_with_info.side_effect = grab
        cam.buffer_download(
            n_frames=1,
            convert=False,
            strip_header=False,
            verbose=False,
            retries=0,
            resend=want,
        )
        assert captured["resend"] is want


def test_buffer_download_resets_stats_on_empty_buffer():
    cam = _fake_cam_for_download()
    # First, a normal download populates stats.
    cam._gvsp.get_frame_with_info.side_effect = [_frame(0, 0), None]
    cam.buffer_download(
        n_frames=1, convert=False, strip_header=False, verbose=False, retries=0
    )
    assert cam.last_download_stats is not None
    # Now simulate an empty buffer: read_reg returns 0 for recorded size, so
    # n_frames resolves to 0 and the method returns None early.
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


def test_buffer_download_retries_recover_stragglers():
    cam = _fake_cam_for_download()
    cam._gvsp.get_frame_with_info.side_effect = [_frame(0, 0), _frame(2, 1), None]

    def fake_redownload(frame_ids, packet_size):
        return {
            fid: (np.ones((4, 4), np.uint16), {"block_id": fid, "missing_packets": 0})
            for fid in frame_ids
        }

    cam._redownload_frames = MagicMock(side_effect=fake_redownload)
    out = cam.buffer_download(
        n_frames=2, convert=False, strip_header=False, verbose=False, retries=1
    )
    assert out is not None
    assert out.shape[0] == 2
    assert cam.last_download_stats.n_incomplete == 0
    assert cam.last_download_stats.recovered_by_retry == 1
    cam._redownload_frames.assert_called_once()


def test_buffer_download_falls_back_when_jumbo_unsupported(caplog):
    cam = _fake_cam_for_download()
    cam._gvsp.get_frame_with_info.side_effect = [_frame(0, 0), _frame(0, 1), None]
    cam._probe_max_packet_size = MagicMock(return_value=1500)
    with caplog.at_level(logging.WARNING, logger="pyTelops.camera"):
        cam.buffer_download(
            n_frames=2,
            convert=False,
            strip_header=False,
            verbose=False,
            retries=0,
            packet_size=9000,
        )
    assert cam.last_download_stats.packet_size_used == 1500
    assert any("1500" in r.message for r in caplog.records)


def test_public_exports():
    import pyTelops

    assert hasattr(pyTelops, "FrameIntegrityError")
    assert hasattr(pyTelops, "DownloadStats")
    assert hasattr(pyTelops, "tune_connection")
    assert hasattr(pyTelops, "ConnectionReport")
