import logging
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from pyGigEVision import GVCPError

from pyTelops import registers as reg
from pyTelops.camera import (
    Camera,
    _group_contiguous,
    _learn_bitrate,
    _missing_positions,
    _resolve_packet_size,
    _timestamp_order_report,
)
from pyTelops.errors import DownloadStats, FrameIntegrityError


def test_download_stats_has_first_pass_field():
    s = DownloadStats(n_frames=5)
    assert s.first_pass_n_complete == 0


class TestLearnBitrate:
    def test_clean_first_pass_keeps_bitrate(self):
        assert _learn_bitrate(1.0, 1000) == 1000.0

    def test_lossy_first_pass_halves(self):
        assert _learn_bitrate(0.5, 1000) == 500.0

    def test_clamped_to_floor(self):
        assert _learn_bitrate(0.0, 150, floor=100.0) == 100.0


def test_camera_auto_tune_defaults_on():
    cam = Camera()
    assert cam.auto_tune is True
    assert cam._jumbo_probed is False


def test_reset_auto_tune_cache_clears_state():
    cam = Camera()
    cam.recommended_download_kwargs = {"packet_size": 9000, "bitrate_mbps": 250}
    cam._jumbo_probed = True
    cam._reset_auto_tune_cache()
    assert cam.recommended_download_kwargs == {}
    assert cam._jumbo_probed is False


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
    assert calls[1][2] == 1000.0  # recovery stays at base (lowering it causes striding)


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


def test_buffer_download_explicit_packet_size_overrides_cache():
    cam = _fake_cam_for_download()
    cam.recommended_download_kwargs = {"packet_size": 9000}
    seen = {}

    def dr(frame_id, count, *, packet_size, **kw):
        seen["ps"] = packet_size
        return _complete_range(frame_id, count)

    cam._download_range = MagicMock(side_effect=dr)
    cam.buffer_download(
        n_frames=2, packet_size=1500, convert=False, strip_header=False, verbose=False
    )
    assert seen["ps"] == 1500


def test_buffer_download_uses_cached_packet_size_and_bitrate():
    cam = _fake_cam_for_download()
    cam.recommended_download_kwargs = {"packet_size": 9000, "bitrate_mbps": 250}
    seen = {}

    def dr(frame_id, count, *, packet_size, bitrate_mbps, **kw):
        seen["ps"], seen["br"] = packet_size, bitrate_mbps
        return _complete_range(frame_id, count)

    cam._download_range = MagicMock(side_effect=dr)
    cam.buffer_download(n_frames=2, convert=False, strip_header=False, verbose=False)
    assert seen["ps"] == 9000
    assert seen["br"] == 250


def test_buffer_download_auto_tune_off_uses_defaults():
    cam = _fake_cam_for_download()
    cam.auto_tune = False
    seen = {}

    def dr(frame_id, count, *, packet_size, bitrate_mbps, **kw):
        seen["ps"], seen["br"] = packet_size, bitrate_mbps
        return _complete_range(frame_id, count)

    cam._download_range = MagicMock(side_effect=dr)
    cam._probe_max_packet_size = MagicMock()
    cam.buffer_download(n_frames=2, convert=False, strip_header=False, verbose=False)
    assert seen["ps"] == 1500
    assert seen["br"] == 1000.0
    cam._probe_max_packet_size.assert_not_called()


def test_buffer_download_probes_jumbo_once_then_caches():
    cam = _fake_cam_for_download()
    cam._probe_max_packet_size = MagicMock(return_value=9000)
    cam._download_range = MagicMock(side_effect=_complete_range)
    cam.buffer_download(n_frames=2, convert=False, strip_header=False, verbose=False)
    cam.buffer_download(n_frames=2, convert=False, strip_header=False, verbose=False)
    cam._probe_max_packet_size.assert_called_once()  # second call uses the cache
    assert cam.recommended_download_kwargs["packet_size"] == 9000


def test_buffer_download_records_first_pass_complete():
    cam = _fake_cam_for_download()

    def dr(frame_id, count, **kw):
        if frame_id == 0 and count == 4:  # first pass: 3 of 4 arrive
            return {off: (np.ones((4, 4), np.uint16), {"missing_packets": 0}) for off in (0, 1, 3)}
        return _complete_range(frame_id, count)

    cam._download_range = MagicMock(side_effect=dr)
    cam.buffer_download(n_frames=4, convert=False, strip_header=False, verbose=False)
    assert cam.last_download_stats.first_pass_n_complete == 3
    assert cam.last_download_stats.n_incomplete == 0


def test_buffer_download_lowers_learned_bitrate_after_drops():
    cam = _fake_cam_for_download()

    def dr(frame_id, count, **kw):
        if frame_id == 0 and count == 10:  # first pass: only half arrive
            return {off: (np.ones((4, 4), np.uint16), {"missing_packets": 0}) for off in range(5)}
        return _complete_range(frame_id, count)

    cam._download_range = MagicMock(side_effect=dr)
    cam.buffer_download(n_frames=10, convert=False, strip_header=False, verbose=False)
    assert cam.recommended_download_kwargs["bitrate_mbps"] == 500.0  # 1000 halved


def test_buffer_download_explicit_bitrate_not_learned():
    cam = _fake_cam_for_download()

    def dr(frame_id, count, **kw):
        if frame_id == 0 and count == 10:
            return {off: (np.ones((4, 4), np.uint16), {"missing_packets": 0}) for off in range(5)}
        return _complete_range(frame_id, count)

    cam._download_range = MagicMock(side_effect=dr)
    cam.buffer_download(
        n_frames=10, bitrate_mbps=1000, convert=False, strip_header=False, verbose=False
    )
    assert "bitrate_mbps" not in cam.recommended_download_kwargs


def test_download_range_forces_packet_delay_zero():
    # A high packet_delay (SCPD) makes the camera silently DECIMATE the buffer
    # download by 2 (confirmed on hardware). _download_range must force SCPD to 0
    # for the transfer regardless of the user's live streaming packet_delay, and
    # restore it afterwards.
    from pyGigEVision.standard import REG_SC_PACKET_DELAY

    cam = _fake_cam_for_download()
    cam._packet_delay_override = 1000
    delay_writes = []

    def rd(addr):
        return 1000 if addr == REG_SC_PACKET_DELAY else 0

    def wr(addr, value):
        if addr == REG_SC_PACKET_DELAY:
            delay_writes.append(value)

    cam._gvcp.read_reg.side_effect = rd
    cam._gvcp.write_reg.side_effect = wr
    cam._gvcp.read_float.return_value = 1000.0
    cam._gvsp.get_frame_with_info.side_effect = [
        (np.ones((4, 4), np.uint16), {"block_id": 1, "missing_packets": 0}),
        None,
    ]
    with patch("pyTelops.camera.time.sleep"):
        cam._download_range(0, 1, packet_size=1500, bitrate_mbps=500, resend=False, timeout=5)
    assert 0 in delay_writes, "download must force packet_delay (SCPD) to 0"
    assert delay_writes[-1] == 1000, "must restore the user's packet_delay after the download"


def test_buffer_download_splits_into_chunks():
    # A large download must be streamed in <= chunk_size sessions: one huge
    # session overruns the host receive path and loses/mis-orders most frames,
    # while small sessions come back complete.
    cam = _fake_cam_for_download()
    calls = []

    def dr(frame_id, count, **kw):
        calls.append((frame_id, count))
        return {
            off: (np.full((4, 4), frame_id + off, np.uint16), {"missing_packets": 0})
            for off in range(count)
        }

    cam._download_range = MagicMock(side_effect=dr)
    out = cam.buffer_download(
        n_frames=2500, chunk_size=1000, convert=False, strip_header=False, verbose=False
    )
    # First pass: 3 sessions of at most 1000 frames (1000 + 1000 + 500).
    assert [c[1] for c in calls] == [1000, 1000, 500]
    assert [c[0] for c in calls] == [0, 1000, 2000]  # consecutive, correctly offset
    assert out.shape[0] == 2500
    # Assembled in global frame order across chunk boundaries.
    assert [int(out[i, 0, 0]) for i in (0, 999, 1000, 2499)] == [0, 999, 1000, 2499]
    assert cam.last_download_stats.n_incomplete == 0


def test_buffer_download_recovers_missing_across_chunks_in_order():
    # A frame dropped in one chunk's first pass is re-streamed (still <= chunk)
    # and lands at its correct global position.
    cam = _fake_cam_for_download()

    def dr(frame_id, count, **kw):
        if frame_id == 1000 and count == 1000:  # 2nd chunk first pass drops position 1500
            return {
                off: (np.full((4, 4), 1000 + off, np.uint16), {"missing_packets": 0})
                for off in range(1000)
                if off != 500
            }
        return {
            off: (np.full((4, 4), frame_id + off, np.uint16), {"missing_packets": 0})
            for off in range(count)
        }

    cam._download_range = MagicMock(side_effect=dr)
    out = cam.buffer_download(
        n_frames=2500, chunk_size=1000, convert=False, strip_header=False, verbose=False
    )
    assert out.shape[0] == 2500
    assert int(out[1500, 0, 0]) == 1500  # recovered frame at its correct position
    assert cam.last_download_stats.n_incomplete == 0
    assert cam.last_download_stats.recovered_by_retry == 1


def test_download_range_discards_stale_frame_from_prior_session():
    # Under heavy host load the GVSP receiver falls behind and can leave a frame
    # from the PREVIOUS download session queued (or unread in the socket buffer).
    # Block ids restart at 1 each session and _download_range maps
    # offset = block_id - 1, so that stale frame would be read first and mapped
    # to the WRONG position in this range (the out-of-order/strided corruption
    # seen on large downloads under load). The session must flush stale residue
    # before streaming.
    from queue import Queue

    cam = _fake_cam_for_download()
    q: Queue = Queue()
    # A stale frame left over from a prior session: block_id 3, marker pixel 999.
    q.put(
        (np.full((4, 4), 999, np.uint16), {"block_id": 3, "missing_packets": 0, "timestamp": 500})
    )

    cam._gvsp.get_frame_with_info.side_effect = lambda timeout=5.0: (
        q.get_nowait() if not q.empty() else None
    )

    def flush():
        n = 0
        while not q.empty():
            q.get_nowait()
            n += 1
        return n

    cam._gvsp.flush.side_effect = flush

    # The camera streams this range's 3 fresh frames when acquisition starts.
    def wr(addr, value):
        if addr == reg.REG_ACQUISITION_START and value == 1:
            for bid in (1, 2, 3):
                q.put(
                    (
                        np.full((4, 4), bid, np.uint16),
                        {"block_id": bid, "missing_packets": 0, "timestamp": 1000 + bid},
                    )
                )

    cam._gvcp.write_reg.side_effect = wr
    cam._gvcp.read_reg.return_value = 0
    cam._gvcp.read_float.return_value = 1000.0

    with patch("pyTelops.camera.time.sleep"):
        got = cam._download_range(
            100, 3, packet_size=1500, bitrate_mbps=500, resend=False, timeout=5
        )

    # Only the 3 fresh frames, each at its correct offset; the stale block_id-3
    # frame (pixel 999) must not have been mapped onto position 2.
    assert set(got.keys()) == {0, 1, 2}
    assert int(got[0][0][0, 0]) == 1
    assert int(got[1][0][0, 0]) == 2
    assert int(got[2][0][0, 0]) == 3
    assert all(int(frame[0, 0]) != 999 for frame, _info in got.values())


def test_download_range_retries_transient_setup_write():
    # Under extreme host load a control register write during download setup can
    # transiently return GENERIC_ERROR; it must be retried, not abort the download.
    cam = _fake_cam_for_download()
    state = {"mode_writes": 0}

    def wr(addr, value):
        # Only the SEQUENCE setup write (not the OFF write in cleanup).
        if (
            addr == reg.REG_MEMORY_BUFFER_DOWNLOAD_MODE
            and value == reg.MemoryBufferDownloadMode.SEQUENCE
        ):
            state["mode_writes"] += 1
            if state["mode_writes"] == 1:
                raise GVCPError("Command 0x0082 failed", 2)

    cam._gvcp.write_reg.side_effect = wr
    cam._gvsp.get_frame_with_info.side_effect = [
        (np.ones((4, 4), np.uint16), {"block_id": 1, "missing_packets": 0}),
        None,
    ]
    with patch("pyTelops.camera.time.sleep"):
        got = cam._download_range(
            0, 1, packet_size=1500, bitrate_mbps=1000, resend=False, timeout=5
        )
    assert state["mode_writes"] == 2  # the transient failure was retried
    assert 0 in got  # frame still downloaded


def test_public_exports():
    import pyTelops

    assert hasattr(pyTelops, "FrameIntegrityError")
    assert hasattr(pyTelops, "DownloadStats")
    assert hasattr(pyTelops, "tune_connection")
    assert hasattr(pyTelops, "ConnectionReport")


def _range_with_ts(pos_to_ts):
    """Build a ``_download_range`` return dict from ``{position: timestamp}``.

    Every frame is packet-complete; only the leader timestamp varies, so these
    exercise the ordering check without any packet loss.
    """
    return {
        pos: (np.full((4, 4), pos, np.uint16), {"missing_packets": 0, "timestamp": ts})
        for pos, ts in pos_to_ts.items()
    }


def test_buffer_download_raises_on_strided_order():
    # The large-download bug: the camera streams every other buffer frame
    # (stride-2) for the first pass, then the paced recovery re-streams the tail
    # by absolute frame id. Every frame is packet-complete, so the old
    # completeness-only check passed silently. The leader timestamps expose it:
    # the first block steps by 2x the frame period and the tail restarts the
    # timeline, so the timestamp drops back (a non-monotonic step) at the splice.
    cam = _fake_cam_for_download()
    order = {0: 1000, 1: 1020, 2: 1040, 3: 1060, 4: 1030, 5: 1040, 6: 1050, 7: 1060}
    cam._download_range = MagicMock(side_effect=lambda frame_id, count, **kw: _range_with_ts(order))
    with pytest.raises(FrameIntegrityError, match="order"):
        cam.buffer_download(n_frames=8, convert=False, strip_header=False, verbose=False)
    assert cam.last_download_stats.n_out_of_order >= 1


def test_buffer_download_raises_on_strided_first_pass_then_recovered_tail():
    # Faithful reproduction of the reported bug through the real recovery loop:
    # the first full-range pass returns every other buffer frame (stride-2,
    # timestamps for phys 0,2,4,6) and drops the rest; the paced recovery then
    # re-streams the tail by absolute frame id (phys 4,5,6,7). The assembled
    # array is packet-complete but its timestamps drop back at the splice.
    cam = _fake_cam_for_download()

    def dr(frame_id, count, **kw):
        if frame_id == 0 and count == 8:  # first pass: stride-2, only positions 0..3
            return {
                off: (
                    np.full((4, 4), off, np.uint16),
                    {"missing_packets": 0, "timestamp": 1000 + 20 * off},
                )
                for off in range(4)
            }
        # recovery of the missing tail positions 4..7, addressed by absolute id
        return {
            off: (
                np.full((4, 4), frame_id + off, np.uint16),
                {"missing_packets": 0, "timestamp": 1000 + 10 * (frame_id + off)},
            )
            for off in range(count)
        }

    cam._download_range = MagicMock(side_effect=dr)
    with pytest.raises(FrameIntegrityError, match="order"):
        cam.buffer_download(n_frames=8, convert=False, strip_header=False, verbose=False)
    assert cam.last_download_stats.n_incomplete == 0  # every frame was packet-complete
    assert cam.last_download_stats.n_out_of_order >= 1


def test_buffer_download_clean_timestamps_pass():
    cam = _fake_cam_for_download()
    order = {i: 1000 + 10 * i for i in range(8)}  # evenly spaced, monotonic
    cam._download_range = MagicMock(side_effect=lambda frame_id, count, **kw: _range_with_ts(order))
    out = cam.buffer_download(n_frames=8, convert=False, strip_header=False, verbose=False)
    assert out.shape[0] == 8
    assert cam.last_download_stats.n_out_of_order == 0
    assert cam.last_download_stats.n_stride_gaps == 0


def _dr_with_one_order_blip(n):
    """A ``_download_range`` mock returning *n* clean frames with a single
    backward timestamp blip at position 50 (~2 anomalies -> ~2% for n=100)."""
    ts = {i: 1000 + 10 * i for i in range(n)}
    ts[50] = 1000 + 10 * 48  # dip below its neighbours -> 1 out-of-order + 1 stride

    def dr(frame_id, count, **kw):
        return {
            off: (
                np.full((4, 4), frame_id + off, np.uint16),
                {"missing_packets": 0, "timestamp": ts[frame_id + off]},
            )
            for off in range(count)
        }

    return dr


def test_buffer_download_tolerates_minor_order_residual():
    # A small mis-ordered fraction (below order_tolerance) is recorded and logged
    # but does NOT raise -- large chunked downloads carry an irreducible ~1%
    # cross-session residual that should not fail every download.
    cam = _fake_cam_for_download()
    cam._download_range = MagicMock(side_effect=_dr_with_one_order_blip(100))
    out = cam.buffer_download(n_frames=100, convert=False, strip_header=False, verbose=False)
    assert out is not None and out.shape[0] == 100  # did not raise
    assert cam.last_download_stats.n_out_of_order >= 1  # residual still recorded


def test_buffer_download_order_tolerance_zero_is_strict():
    cam = _fake_cam_for_download()
    cam._download_range = MagicMock(side_effect=_dr_with_one_order_blip(100))
    with pytest.raises(FrameIntegrityError, match="order"):
        cam.buffer_download(
            n_frames=100, order_tolerance=0.0, convert=False, strip_header=False, verbose=False
        )


def test_buffer_download_gross_decimation_still_raises_under_tolerance():
    # ~half the frames strided (packet_delay/low-bitrate decimation) exceeds any
    # sane tolerance and must still raise.
    cam = _fake_cam_for_download()
    # stride-2: every position two periods apart, then the tail restarts
    order = {i: 1000 + 20 * i for i in range(50)}
    order.update({50 + i: 1000 + 10 * i for i in range(50)})  # backward splice
    cam._download_range = MagicMock(side_effect=lambda frame_id, count, **kw: _range_with_ts(order))
    with pytest.raises(FrameIntegrityError, match="order"):
        cam.buffer_download(n_frames=100, convert=False, strip_header=False, verbose=False)


def test_buffer_download_verify_order_false_bypasses():
    cam = _fake_cam_for_download()
    order = {0: 1000, 1: 1020, 2: 1040, 3: 1060, 4: 1030, 5: 1040, 6: 1050, 7: 1060}
    cam._download_range = MagicMock(side_effect=lambda frame_id, count, **kw: _range_with_ts(order))
    out = cam.buffer_download(
        n_frames=8, verify_order=False, convert=False, strip_header=False, verbose=False
    )
    assert out.shape[0] == 8  # corruption is still reported in stats, but not raised
    assert cam.last_download_stats.n_out_of_order >= 1


class TestTimestampOrderReport:
    def test_clean_monotonic_is_ok(self):
        assert _timestamp_order_report([0, 1, 2, 3, 4], [1000, 1010, 1020, 1030, 1040]) == (0, 0)

    def test_backward_step_and_stride_flagged(self):
        # first block stride-2 (step 20), tail restarts the timeline (step back)
        n_back, n_stride = _timestamp_order_report(
            [0, 1, 2, 3, 4, 5, 6, 7], [1000, 1020, 1040, 1060, 1030, 1040, 1050, 1060]
        )
        assert n_back >= 1
        assert n_stride >= 1

    def test_tolerated_drops_not_flagged(self):
        # positions 2 and 5 dropped (tolerated); the present frames are still
        # evenly spaced once normalised by the position gap -> no false positive.
        assert _timestamp_order_report(
            [0, 1, 3, 4, 6, 7], [1000, 1010, 1030, 1040, 1060, 1070]
        ) == (0, 0)

    def test_absent_timestamps_skip(self):
        assert _timestamp_order_report([0, 1, 2, 3], [0, 0, 0, 0]) == (0, 0)

    def test_too_few_frames_skip(self):
        assert _timestamp_order_report([0, 1], [5, 6]) == (0, 0)
