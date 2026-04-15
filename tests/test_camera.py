"""Tests for Camera class.

Unit tests use mocking. Hardware tests require --hardware flag.
"""

import numpy as np
import pytest
from unittest.mock import MagicMock, patch

from pyTelops.camera import Camera, discover, _find_link_local_ip
from pyTelops import registers as reg


def _make_fake_connected_camera():
    """Return a Camera wired with mock GVCP/GVSP, bypassing the network.

    The mock GVCP returns 0 for any read (so _check_ready treats the
    camera as ready) and silently accepts writes. The mock GVSP's
    socket returns a valid IP/port tuple so start_stream() can run
    end-to-end without real network I/O.
    """
    cam = Camera()
    cam._connected = True
    cam._streaming = False
    cam._acquiring = False
    cam._local_ip = "169.254.1.1"
    cam._gvcp = MagicMock()
    cam._gvcp.read_reg.return_value = 0
    cam._gvcp._control_lost = False
    cam._gvsp = MagicMock()
    cam._gvsp.get_frame.return_value = None
    cam._gvsp.port = 3957
    cam._gvsp._sock.getsockname.return_value = ("169.254.1.1", 3957)
    return cam


class TestCameraInit:
    """Test Camera construction (no network)."""

    def test_default_init(self):
        cam = Camera()
        assert cam._camera_ip is None
        assert not cam.is_connected
        assert not cam.is_streaming
        assert not cam.is_acquiring

    def test_init_with_ip(self):
        cam = Camera(ip="169.254.1.1")
        assert cam._camera_ip == "169.254.1.1"

    def test_repr_disconnected(self):
        cam = Camera(ip="169.254.1.1")
        assert "disconnected" in repr(cam)

    def test_not_connected_raises(self):
        cam = Camera()
        with pytest.raises(RuntimeError, match="not connected"):
            cam.grab()

    def test_properties_raise_when_disconnected(self):
        cam = Camera()
        with pytest.raises(RuntimeError):
            _ = cam.integration_time
        with pytest.raises(RuntimeError):
            _ = cam.frame_rate
        with pytest.raises(RuntimeError):
            _ = cam.info

    def test_acquisition_start_raises_when_disconnected(self):
        cam = Camera()
        with pytest.raises(RuntimeError, match="not connected"):
            cam.acquisition_start()

    def test_read_frame_raises_when_disconnected(self):
        cam = Camera()
        with pytest.raises(RuntimeError, match="acquisition not active"):
            cam.read_frame()


class TestAcquisitionAPI:
    """Unit tests for acquisition_start/stop/contextmanager/read_frame.

    Uses a fake connected camera with mocked GVCP/GVSP — no network.
    """

    def test_is_acquiring_starts_false(self):
        cam = _make_fake_connected_camera()
        assert cam.is_acquiring is False

    def test_acquisition_start_sets_flag_and_writes_register(self):
        cam = _make_fake_connected_camera()
        with patch.object(cam, "start_stream") as mock_start:
            cam.acquisition_start()
            mock_start.assert_called_once()
        cam._gvcp.write_reg.assert_any_call(reg.REG_ACQUISITION_START, 1)
        assert cam.is_acquiring is True

    def test_acquisition_start_idempotent(self):
        cam = _make_fake_connected_camera()
        with patch.object(cam, "start_stream"):
            cam.acquisition_start()
            n_writes = cam._gvcp.write_reg.call_count
            cam.acquisition_start()
            cam.acquisition_start()
        # No additional writes after the first start
        assert cam._gvcp.write_reg.call_count == n_writes
        assert cam.is_acquiring is True

    def test_acquisition_start_skips_start_stream_if_already_streaming(self):
        cam = _make_fake_connected_camera()
        cam._streaming = True
        with patch.object(cam, "start_stream") as mock_start:
            cam.acquisition_start()
            mock_start.assert_not_called()
        cam._gvcp.write_reg.assert_any_call(reg.REG_ACQUISITION_START, 1)

    def test_acquisition_stop_clears_flag_and_writes_register(self):
        cam = _make_fake_connected_camera()
        with patch.object(cam, "start_stream"):
            cam.acquisition_start()
        cam._gvcp.reset_mock()
        cam.acquisition_stop()
        cam._gvcp.write_reg.assert_any_call(reg.REG_ACQUISITION_STOP, 1)
        assert cam.is_acquiring is False

    def test_acquisition_stop_idempotent(self):
        cam = _make_fake_connected_camera()
        # Stop when not acquiring should be a no-op
        cam.acquisition_stop()
        cam.acquisition_stop()
        # No register writes for ACQUISITION_STOP because flag was False
        for call in cam._gvcp.write_reg.call_args_list:
            assert call.args[0] != reg.REG_ACQUISITION_STOP

    def test_acquisition_contextmanager_starts_and_stops(self):
        cam = _make_fake_connected_camera()
        with patch.object(cam, "start_stream"):
            with cam.acquisition() as c:
                assert c is cam
                assert cam.is_acquiring is True
        assert cam.is_acquiring is False

    def test_acquisition_contextmanager_stops_on_exception(self):
        cam = _make_fake_connected_camera()
        with patch.object(cam, "start_stream"):
            with pytest.raises(ValueError):
                with cam.acquisition():
                    assert cam.is_acquiring is True
                    raise ValueError("oops")
        assert cam.is_acquiring is False

    def test_read_frame_raises_without_active_acquisition(self):
        cam = _make_fake_connected_camera()
        with pytest.raises(RuntimeError, match="acquisition not active"):
            cam.read_frame()

    def test_read_frame_returns_none_on_empty_queue(self):
        cam = _make_fake_connected_camera()
        with patch.object(cam, "start_stream"):
            cam.acquisition_start()
        cam._gvsp.get_frame.return_value = None
        result = cam.read_frame(timeout=0.1)
        assert result is None

    def test_read_frame_strips_headers_when_convert_false(self):
        cam = _make_fake_connected_camera()
        # Fake raw frame: 2 header rows + 4 data rows of 8 cols
        raw = np.zeros((6, 8), dtype=np.uint16)
        raw[2:, :] = 42
        cam._gvsp.get_frame.return_value = raw
        with patch.object(cam, "start_stream"):
            cam.acquisition_start()
        result = cam.read_frame(timeout=0.0, convert=False, strip_header=True)
        assert result.shape == (4, 8)
        assert (result == 42).all()

    def test_read_frame_latest_drains_queue(self):
        """latest=True must return the newest frame, discarding older ones."""
        cam = _make_fake_connected_camera()
        # Three frames in queue, then None — latest should be frame3
        frame1 = np.full((6, 8), 10, dtype=np.uint16)
        frame2 = np.full((6, 8), 20, dtype=np.uint16)
        frame3 = np.full((6, 8), 30, dtype=np.uint16)
        cam._gvsp.get_frame.side_effect = [frame1, frame2, frame3, None]
        with patch.object(cam, "start_stream"):
            cam.acquisition_start()
        result = cam.read_frame(latest=True, convert=False, strip_header=False)
        # All three frames pulled, newest returned
        assert cam._gvsp.get_frame.call_count == 4  # 3 frames + 1 None
        assert (result == 30).all()  # newest

    def test_read_frame_latest_blocks_when_queue_empty(self):
        """latest=True with empty queue should block briefly for a frame
        if timeout > 0."""
        cam = _make_fake_connected_camera()
        fresh = np.full((6, 8), 99, dtype=np.uint16)
        # First call (drain attempt): None. Second call (blocking): a frame.
        cam._gvsp.get_frame.side_effect = [None, fresh]
        with patch.object(cam, "start_stream"):
            cam.acquisition_start()
        result = cam.read_frame(latest=True, timeout=0.1,
                                convert=False, strip_header=False)
        assert (result == 99).all()
        assert cam._gvsp.get_frame.call_count == 2

    def test_read_frame_latest_non_blocking_returns_none(self):
        """latest=True with empty queue and timeout=0 returns None."""
        cam = _make_fake_connected_camera()
        cam._gvsp.get_frame.return_value = None
        with patch.object(cam, "start_stream"):
            cam.acquisition_start()
        result = cam.read_frame(latest=True, timeout=0.0)
        assert result is None

    def test_read_frame_default_preserves_order(self):
        """Without latest=True, the existing behavior is unchanged: one
        call to get_frame, returns whatever it returns."""
        cam = _make_fake_connected_camera()
        frame1 = np.zeros((6, 8), dtype=np.uint16)
        cam._gvsp.get_frame.return_value = frame1
        with patch.object(cam, "start_stream"):
            cam.acquisition_start()
        cam._gvsp.get_frame.reset_mock()
        cam.read_frame(timeout=0.1, convert=False, strip_header=False)
        # Single call — no drain loop
        assert cam._gvsp.get_frame.call_count == 1

    def test_read_frame_calls_apply_calibration_when_convert_true(self):
        cam = _make_fake_connected_camera()
        fake_raw = np.zeros((6, 8), dtype=np.uint16)
        fake_calibrated = np.full((4, 8), 25.0, dtype=np.float32)
        cam._gvsp.get_frame.return_value = fake_raw
        with patch.object(cam, "start_stream"):
            cam.acquisition_start()
        with patch.object(cam, "_apply_calibration",
                          return_value=fake_calibrated) as mock_cal:
            result = cam.read_frame(timeout=0.0, convert=True)
        mock_cal.assert_called_once()
        assert result.shape == (4, 8)
        assert (result == 25.0).all()

    def test_grab_uses_acquisition_lifecycle(self):
        """grab() should set _acquiring during the call and clear it after."""
        cam = _make_fake_connected_camera()
        cam._gvsp.get_frame.return_value = None  # timeout
        with patch.object(cam, "start_stream"), \
                patch.object(cam, "stop_stream"):
            cam.grab(timeout=0.0)
        assert cam.is_acquiring is False  # restored

    def test_grab_inside_acquisition_does_not_stop_acquisition(self):
        """grab() inside an acquisition() block must leave acquisition running."""
        cam = _make_fake_connected_camera()
        cam._gvsp.get_frame.return_value = None
        with patch.object(cam, "start_stream"), \
                patch.object(cam, "stop_stream"):
            with cam.acquisition():
                cam.grab(timeout=0.0)
                assert cam.is_acquiring is True
        assert cam.is_acquiring is False

    def test_acquire_uses_acquisition_lifecycle(self):
        cam = _make_fake_connected_camera()
        cam._gvsp.get_frame.return_value = None
        with patch.object(cam, "start_stream"), \
                patch.object(cam, "stop_stream"):
            cam.acquire(n_frames=3, timeout=0.0)
        assert cam.is_acquiring is False

    def test_stop_stream_also_stops_acquisition(self):
        cam = _make_fake_connected_camera()
        cam._streaming = True
        with patch.object(cam, "start_stream"):
            cam.acquisition_start()
        # Stop stream without explicitly stopping acquisition first
        cam.stop_stream()
        assert cam.is_acquiring is False
        assert cam.is_streaming is False

    def test_roi_offset_rejects_misaligned_x(self):
        """Client-side validation: offset_x must be a multiple of WIDTH_STEP (64)."""
        cam = _make_fake_connected_camera()
        # Stub resolution so the subwindow fit check doesn't fire first
        with patch.object(type(cam), "resolution",
                          new=property(lambda self: (64, 4))):
            with pytest.raises(ValueError, match="multiple of 64"):
                cam.roi_offset = (96, 0)

    def test_roi_offset_rejects_misaligned_y(self):
        """Client-side validation: offset_y must be a multiple of HEIGHT_STEP (4)."""
        cam = _make_fake_connected_camera()
        with patch.object(type(cam), "resolution",
                          new=property(lambda self: (64, 4))):
            with pytest.raises(ValueError, match="multiple of 4"):
                cam.roi_offset = (0, 3)

    def test_roi_offset_rejects_negative(self):
        cam = _make_fake_connected_camera()
        with patch.object(type(cam), "resolution",
                          new=property(lambda self: (64, 4))):
            with pytest.raises(ValueError, match="non-negative"):
                cam.roi_offset = (-64, 0)

    def test_roi_offset_rejects_out_of_bounds(self):
        """x + width must fit within sensor width."""
        cam = _make_fake_connected_camera()
        with patch.object(type(cam), "resolution",
                          new=property(lambda self: (128, 64))):
            with pytest.raises(ValueError, match="exceeds sensor width"):
                cam.roi_offset = (256, 0)  # 256 + 128 = 384 > 320

    def test_roi_offset_accepts_valid_values(self):
        cam = _make_fake_connected_camera()
        with patch.object(type(cam), "resolution",
                          new=property(lambda self: (128, 64))):
            cam.roi_offset = (64, 96)
            cam._gvcp.write_reg.assert_any_call(reg.REG_OFFSET_X, 64)
            cam._gvcp.write_reg.assert_any_call(reg.REG_OFFSET_Y, 96)

    def test_packet_delay_default_is_zero_override_none(self):
        cam = _make_fake_connected_camera()
        # Override flag starts None = "use default 0 in start_stream"
        assert cam._packet_delay_override is None

    def test_packet_delay_getter_reads_register(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = 1234
        assert cam.packet_delay == 1234
        cam._gvcp.read_reg.assert_called_with(reg.REG_SC_PACKET_DELAY)

    def test_packet_delay_setter_writes_register_and_override(self):
        cam = _make_fake_connected_camera()
        cam.packet_delay = 1000
        cam._gvcp.write_reg.assert_any_call(reg.REG_SC_PACKET_DELAY, 1000)
        assert cam._packet_delay_override == 1000

    def test_packet_delay_setter_rejects_negative(self):
        cam = _make_fake_connected_camera()
        with pytest.raises(ValueError, match="non-negative"):
            cam.packet_delay = -1

    def test_packet_delay_setter_coerces_int(self):
        cam = _make_fake_connected_camera()
        cam.packet_delay = 500.7  # float — should be coerced
        assert cam._packet_delay_override == 500

    def test_start_stream_forces_zero_when_no_override(self):
        """Backward compat: unchanged default behavior when user doesn't
        touch packet_delay. start_stream forces the register to 0."""
        cam = _make_fake_connected_camera()
        # Simulate a non-zero persistent camera setting
        cam._gvcp.read_reg.return_value = 5000
        cam.start_stream()
        # Should have written 0 because override is None (default behavior)
        cam._gvcp.write_reg.assert_any_call(reg.REG_SC_PACKET_DELAY, 0)
        assert cam._packet_delay_override is None  # still untouched

    def test_start_stream_respects_override(self):
        """New behavior: if user set packet_delay, start_stream uses it
        instead of forcing to 0."""
        cam = _make_fake_connected_camera()
        cam.packet_delay = 1000  # user sets an override
        # Simulate register being reset to 0 externally (e.g., after
        # a previous stop_stream or a camera reset)
        cam._gvcp.read_reg.return_value = 0
        cam._gvcp.reset_mock()
        cam.start_stream()
        # Should re-apply the user's override, NOT force to 0
        write_calls = [call for call in cam._gvcp.write_reg.call_args_list
                       if call.args[0] == reg.REG_SC_PACKET_DELAY]
        assert len(write_calls) == 1
        assert write_calls[0].args[1] == 1000

    def test_start_stream_skips_rewrite_if_override_matches(self):
        """Optimization: don't rewrite register if it's already at the
        target value (whether override or default 0)."""
        cam = _make_fake_connected_camera()
        cam.packet_delay = 1000  # triggers one write in the setter
        cam._gvcp.reset_mock()
        # Camera is already at 1000
        cam._gvcp.read_reg.return_value = 1000
        cam.start_stream()
        # start_stream should NOT have written REG_SC_PACKET_DELAY again
        delay_writes = [call for call in cam._gvcp.write_reg.call_args_list
                        if call.args[0] == reg.REG_SC_PACKET_DELAY]
        assert len(delay_writes) == 0

    def test_packet_delay_survives_stream_restart(self):
        """The user override persists across stop_stream/start_stream."""
        cam = _make_fake_connected_camera()
        cam.packet_delay = 1500
        assert cam._packet_delay_override == 1500
        # Fake a stop and a new start
        cam._streaming = False
        cam._gvcp.read_reg.return_value = 0  # as if register was reset
        cam._gvcp.reset_mock()
        cam.start_stream()
        # Override should have been re-applied
        cam._gvcp.write_reg.assert_any_call(reg.REG_SC_PACKET_DELAY, 1500)
        # Override flag is still set for future restarts
        assert cam._packet_delay_override == 1500

    def test_grab_cleans_up_stream_if_acquisition_start_raises(self):
        """Regression test: if write_reg(REG_ACQUISITION_START) raises,
        the previously-started stream socket must still be torn down."""
        from pyTelops.gvcp import GVCPError

        cam = _make_fake_connected_camera()
        # Make start_stream succeed (sets _streaming=True), but the
        # subsequent acquisition register write raises.
        def fake_start_stream():
            cam._streaming = True
        cam._gvcp.write_reg.side_effect = GVCPError("simulated")
        stop_stream_called = []
        with patch.object(cam, "start_stream", side_effect=fake_start_stream), \
                patch.object(cam, "stop_stream",
                             side_effect=lambda: stop_stream_called.append(True)):
            with pytest.raises(GVCPError):
                cam.grab(timeout=0.0)
        assert stop_stream_called, (
            "grab() must call stop_stream() in cleanup if "
            "acquisition_start() raised after start_stream() succeeded")


class TestDiscover:
    """Test discovery function."""

    @patch("pyTelops.camera.GVCPClient.discover")
    def test_discover_returns_list(self, mock_disc):
        mock_disc.return_value = [
            {"ip": "169.254.67.34", "manufacturer": "Telops",
             "model": "FAST M3k"}]
        cameras = discover()
        assert len(cameras) == 1
        assert cameras[0]["ip"] == "169.254.67.34"

    @patch("pyTelops.camera.GVCPClient.discover")
    def test_discover_empty(self, mock_disc):
        mock_disc.return_value = []
        cameras = discover()
        assert cameras == []


# ============================================================
# Hardware tests (skipped without --hardware flag)
# ============================================================

    # Legacy hardware tests removed — all covered by test_hardware.py
