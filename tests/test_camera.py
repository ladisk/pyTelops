"""Tests for Camera class.

Unit tests use mocking. Hardware tests require --hardware flag.
"""

import struct
import warnings
from unittest.mock import ANY, MagicMock, patch

import numpy as np
import pytest
from pyGigEVision import GVCPError
from pyGigEVision.standard import REG_SC_PACKET_DELAY

from pyTelops import registers as reg
from pyTelops.camera import Camera, _parse_download_headers, discover
from pyTelops.header import (
    HDR_FRAME_ID,
    HDR_POSIX_TIME,
    HDR_SIGNATURE,
    HDR_SUBSECOND,
    HDR_XML_MAJOR,
    HDR_XML_MINOR,
    HEADER_BYTES,
    SIGNATURE,
)


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


class TestWriteRegRetry:
    """A transient GVCPError on a register write should be retried.

    The calibration block-load returns GENERIC_ERROR until the camera finishes
    loading the collection; a short retry then succeeds (issue #14).
    """

    def test_retries_on_transient_error_then_succeeds(self):
        cam = _make_fake_connected_camera()
        calls = {"n": 0}

        def wr(addr, value):
            calls["n"] += 1
            if calls["n"] == 1:
                raise GVCPError("Command 0x0082 failed", 2)

        cam._gvcp.write_reg.side_effect = wr
        with patch("pyTelops.camera.time.sleep"):
            cam._write_reg_retry(reg.REG_CAL_BLOCK_LOAD, 1)
        assert calls["n"] == 2  # first failed, retry succeeded

    def test_reraises_after_exhausting_attempts(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.write_reg.side_effect = GVCPError("boom", 2)
        with patch("pyTelops.camera.time.sleep"), pytest.raises(GVCPError):
            cam._write_reg_retry(reg.REG_CAL_BLOCK_LOAD, 1, attempts=3)
        assert cam._gvcp.write_reg.call_count == 3


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
        with patch.object(cam, "start_stream"), cam.acquisition() as c:
            assert c is cam
            assert cam.is_acquiring is True
        assert cam.is_acquiring is False

    def test_acquisition_contextmanager_stops_on_exception(self):
        cam = _make_fake_connected_camera()
        with patch.object(cam, "start_stream"), pytest.raises(ValueError):  # noqa: SIM117
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
        result = cam.read_frame(latest=True, timeout=0.1, convert=False, strip_header=False)
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
        with patch.object(cam, "_apply_calibration", return_value=fake_calibrated) as mock_cal:
            result = cam.read_frame(timeout=0.0, convert=True)
        mock_cal.assert_called_once()
        assert result.shape == (4, 8)
        assert (result == 25.0).all()

    def test_grab_uses_acquisition_lifecycle(self):
        """grab() should set _acquiring during the call and clear it after."""
        cam = _make_fake_connected_camera()
        cam._gvsp.get_frame.return_value = None  # timeout
        with patch.object(cam, "start_stream"), patch.object(cam, "stop_stream"):
            cam.grab(timeout=0.0)
        assert cam.is_acquiring is False  # restored

    def test_grab_inside_acquisition_does_not_stop_acquisition(self):
        """grab() inside an acquisition() block must leave acquisition running."""
        cam = _make_fake_connected_camera()
        cam._gvsp.get_frame.return_value = None
        with patch.object(cam, "start_stream"), patch.object(cam, "stop_stream"), cam.acquisition():
            cam.grab(timeout=0.0)
            assert cam.is_acquiring is True
        assert cam.is_acquiring is False

    def test_acquire_uses_acquisition_lifecycle(self):
        cam = _make_fake_connected_camera()
        cam._gvsp.get_frame.return_value = None
        with patch.object(cam, "start_stream"), patch.object(cam, "stop_stream"):
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

    def test_buffer_clear_reapplies_config(self):
        """buffer_clear must auto-re-apply the last buffer_configure params
        so clear → record → download works without a manual re-configure."""
        cam = _make_fake_connected_camera()
        cam._gvcp.read_float.return_value = 100.0  # fake frame_rate for duration path
        cam.buffer_configure(n_sequences=2, frames_per_seq=500, pre_moi=10, moi_source="software")
        # Verify kwargs were stored
        assert cam._buffer_config_kwargs == {
            "n_sequences": 2,
            "frames_per_seq": 500,
            "pre_moi": 10,
            "moi_source": "software",
        }
        # Count configure-related register writes after the first configure
        configure_writes = [
            call
            for call in cam._gvcp.write_reg.call_args_list
            if call.args[0]
            in (
                reg.REG_MEMORY_BUFFER_NUM_SEQUENCES,
                reg.REG_MEMORY_BUFFER_SEQ_SIZE,
                reg.REG_MEMORY_BUFFER_PRE_MOI_SIZE,
                reg.REG_MEMORY_BUFFER_MOI_SOURCE,
            )
        ]
        n_before_clear = len(configure_writes)
        # Clear — should trigger a re-apply of the same config
        cam.buffer_clear()
        # CLEAR_ALL was written
        cam._gvcp.write_reg.assert_any_call(reg.REG_MEMORY_BUFFER_CLEAR_ALL, 1)
        # And the full configure register block was written again
        configure_writes_after = [
            call
            for call in cam._gvcp.write_reg.call_args_list
            if call.args[0]
            in (
                reg.REG_MEMORY_BUFFER_NUM_SEQUENCES,
                reg.REG_MEMORY_BUFFER_SEQ_SIZE,
                reg.REG_MEMORY_BUFFER_PRE_MOI_SIZE,
                reg.REG_MEMORY_BUFFER_MOI_SOURCE,
            )
        ]
        assert len(configure_writes_after) == n_before_clear + 4, (
            "buffer_clear must re-apply all 4 configure registers"
        )

    def test_buffer_clear_without_prior_configure_is_plain(self):
        """If buffer_configure was never called, buffer_clear should only
        clear and not attempt a re-apply."""
        cam = _make_fake_connected_camera()
        assert cam._buffer_config_kwargs is None
        cam.buffer_clear()
        cam._gvcp.write_reg.assert_any_call(reg.REG_MEMORY_BUFFER_CLEAR_ALL, 1)
        # No num-sequences write — no re-apply happened
        num_seq_writes = [
            call
            for call in cam._gvcp.write_reg.call_args_list
            if call.args[0] == reg.REG_MEMORY_BUFFER_NUM_SEQUENCES
        ]
        assert len(num_seq_writes) == 0

    def test_roi_offset_rejects_misaligned_x(self):
        """Client-side validation: offset_x must be a multiple of WIDTH_STEP (64)."""
        cam = _make_fake_connected_camera()
        # Stub resolution so the subwindow fit check doesn't fire first
        with (
            patch.object(type(cam), "resolution", new=property(lambda self: (64, 4))),
            pytest.raises(ValueError, match="multiple of 64"),
        ):
            cam.roi_offset = (96, 0)

    def test_roi_offset_rejects_misaligned_y(self):
        """Client-side validation: offset_y must be a multiple of HEIGHT_STEP (4)."""
        cam = _make_fake_connected_camera()
        with (
            patch.object(type(cam), "resolution", new=property(lambda self: (64, 4))),
            pytest.raises(ValueError, match="multiple of 4"),
        ):
            cam.roi_offset = (0, 3)

    def test_roi_offset_rejects_negative(self):
        cam = _make_fake_connected_camera()
        with (
            patch.object(type(cam), "resolution", new=property(lambda self: (64, 4))),
            pytest.raises(ValueError, match="non-negative"),
        ):
            cam.roi_offset = (-64, 0)

    def test_roi_offset_rejects_out_of_bounds(self):
        """x + width must fit within sensor width."""
        cam = _make_fake_connected_camera()
        with (
            patch.object(type(cam), "resolution", new=property(lambda self: (128, 64))),
            pytest.raises(ValueError, match="exceeds sensor width"),
        ):
            cam.roi_offset = (256, 0)  # 256 + 128 = 384 > 320

    def test_roi_offset_accepts_valid_values(self):
        cam = _make_fake_connected_camera()
        with patch.object(type(cam), "resolution", new=property(lambda self: (128, 64))):
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
        cam._gvcp.read_reg.assert_called_with(REG_SC_PACKET_DELAY)

    def test_packet_delay_setter_writes_register_and_override(self):
        cam = _make_fake_connected_camera()
        cam.packet_delay = 1000
        cam._gvcp.write_reg.assert_any_call(REG_SC_PACKET_DELAY, 1000)
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
        cam._gvcp.write_reg.assert_any_call(REG_SC_PACKET_DELAY, 0)
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
        write_calls = [
            call
            for call in cam._gvcp.write_reg.call_args_list
            if call.args[0] == REG_SC_PACKET_DELAY
        ]
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
        delay_writes = [
            call
            for call in cam._gvcp.write_reg.call_args_list
            if call.args[0] == REG_SC_PACKET_DELAY
        ]
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
        cam._gvcp.write_reg.assert_any_call(REG_SC_PACKET_DELAY, 1500)
        # Override flag is still set for future restarts
        assert cam._packet_delay_override == 1500

    def test_grab_cleans_up_stream_if_acquisition_start_raises(self):
        """Regression test: if write_reg(REG_ACQUISITION_START) raises,
        the previously-started stream socket must still be torn down."""
        from pyGigEVision import GVCPError

        cam = _make_fake_connected_camera()

        # Make start_stream succeed (sets _streaming=True), but the
        # subsequent acquisition register write raises.
        def fake_start_stream():
            cam._streaming = True

        cam._gvcp.write_reg.side_effect = GVCPError("simulated")
        stop_stream_called = []
        with (
            patch.object(cam, "start_stream", side_effect=fake_start_stream),
            patch.object(cam, "stop_stream", side_effect=lambda: stop_stream_called.append(True)),
            pytest.raises(GVCPError),
        ):
            cam.grab(timeout=0.0)
        assert stop_stream_called, (
            "grab() must call stop_stream() in cleanup if "
            "acquisition_start() raised after start_stream() succeeded"
        )


class TestDiscover:
    """Test discovery function."""

    @patch("pyTelops.camera.GVCPClient.discover")
    def test_discover_returns_list(self, mock_disc):
        mock_disc.return_value = [
            {"ip": "169.254.67.34", "manufacturer": "Telops Inc.", "model": "FAST M3k"}
        ]
        cameras = discover()
        assert len(cameras) == 1
        assert cameras[0]["ip"] == "169.254.67.34"

    @patch("pyTelops.camera.GVCPClient.discover")
    def test_discover_empty(self, mock_disc):
        mock_disc.return_value = []
        cameras = discover()
        assert cameras == []

    @patch("pyTelops.camera.GVCPClient.discover")
    def test_discover_filters_non_telops_by_default(self, mock_disc):
        """discover() must only return Telops devices by default, even
        when other GigE Vision cameras are on the same network."""
        mock_disc.return_value = [
            {
                "ip": "192.168.221.198",
                "manufacturer": "MICRO-EPSILON Optronic GmbH",
                "model": "scanCONTROL 2500-50",
            },
            {"ip": "169.254.67.34", "manufacturer": "Telops Inc.", "model": "TS-IR"},
            {"ip": "169.254.10.1", "manufacturer": "FLIR Systems", "model": "A50"},
        ]
        cameras = discover()
        assert len(cameras) == 1
        assert cameras[0]["manufacturer"] == "Telops Inc."
        assert cameras[0]["ip"] == "169.254.67.34"

    @patch("pyTelops.camera.GVCPClient.discover")
    def test_discover_all_vendors_returns_everything(self, mock_disc):
        """all_vendors=True returns every GigE Vision device found."""
        mock_disc.return_value = [
            {
                "ip": "192.168.221.198",
                "manufacturer": "MICRO-EPSILON Optronic GmbH",
                "model": "scanCONTROL 2500-50",
            },
            {"ip": "169.254.67.34", "manufacturer": "Telops Inc.", "model": "TS-IR"},
        ]
        cameras = discover(all_vendors=True)
        assert len(cameras) == 2

    @patch("pyTelops.camera.GVCPClient.discover")
    def test_discover_filter_with_interface_ip(self, mock_disc):
        """Filter also applies when interface_ip is explicitly passed."""
        mock_disc.return_value = [
            {"ip": "192.168.1.5", "manufacturer": "Basler", "model": "acA2000"},
            {"ip": "192.168.1.6", "manufacturer": "Telops Inc.", "model": "FAST M3k"},
        ]
        cameras = discover(interface_ip="192.168.1.10")
        assert len(cameras) == 1
        assert cameras[0]["manufacturer"] == "Telops Inc."

    @patch("pyTelops.camera.GVCPClient.discover")
    def test_connect_reports_other_vendors_in_error(self, mock_disc):
        """connect() should mention detected non-Telops devices when it
        can't find a Telops camera — makes mixed-vendor setup mistakes
        obvious."""
        mock_disc.return_value = [
            {
                "ip": "192.168.221.198",
                "manufacturer": "MICRO-EPSILON Optronic GmbH",
                "model": "scanCONTROL 2500-50",
            },
        ]
        cam = Camera()
        with pytest.raises(RuntimeError, match="MICRO-EPSILON"):
            cam.connect()


class TestConnectLocalIP:
    """connect() binds the interface the camera replied on during discovery."""

    @patch("pyTelops.camera.GVSPReceiver")
    @patch("pyTelops.camera.GVCPClient")
    @patch("pyTelops.camera._find_local_ip_for")
    @patch("pyTelops.camera.discover")
    def test_connect_binds_discovered_reply_interface(
        self, mock_disc, mock_find, mock_gvcp_cls, mock_gvsp_cls
    ):
        mock_disc.return_value = [
            {
                "ip": "169.254.123.5",
                "manufacturer": "Telops Inc.",
                "model": "TS-IR",
                "reachable": True,
                "interface_ip": "169.254.27.140",
            }
        ]
        mock_gvcp_cls.return_value.read_reg.return_value = 0  # device ready
        cam = Camera()
        try:
            cam.connect()
            mock_gvcp_cls.assert_called_once_with("169.254.123.5", "169.254.27.140", ANY)
            mock_find.assert_not_called()
        finally:
            Camera._active_cameras.clear()

    @patch("pyTelops.camera.GVSPReceiver")
    @patch("pyTelops.camera.GVCPClient")
    @patch("pyTelops.camera._find_local_ip_for", return_value="169.254.9.9")
    @patch("pyTelops.camera.discover")
    def test_connect_falls_back_when_no_interface_ip(
        self, mock_disc, mock_find, mock_gvcp_cls, mock_gvsp_cls
    ):
        mock_disc.return_value = [
            {
                "ip": "169.254.123.5",
                "manufacturer": "Telops Inc.",
                "model": "TS-IR",
                "reachable": True,
            }
        ]
        mock_gvcp_cls.return_value.read_reg.return_value = 0
        cam = Camera()
        try:
            cam.connect()
            mock_find.assert_called_once_with("169.254.123.5")
            mock_gvcp_cls.assert_called_once_with("169.254.123.5", "169.254.9.9", ANY)
        finally:
            Camera._active_cameras.clear()

    @patch("pyTelops.camera.GVSPReceiver")
    @patch("pyTelops.camera.GVCPClient")
    @patch("pyTelops.camera._find_local_ip_for", return_value="169.254.9.9")
    @patch("pyTelops.camera.discover")
    def test_connect_explicit_ip_learns_interface_from_sweep(
        self, mock_disc, mock_find, mock_gvcp_cls, mock_gvsp_cls
    ):
        # An explicit camera IP skips camera selection but still runs a
        # discovery sweep to learn the reply interface; OS routing picks
        # among several link-local interfaces by metric, not reachability.
        mock_disc.return_value = [
            {
                "ip": "169.254.50.50",
                "manufacturer": "Telops Inc.",
                "model": "TS-IR",
                "reachable": True,
                "interface_ip": "169.254.27.140",
            }
        ]
        mock_gvcp_cls.return_value.read_reg.return_value = 0
        cam = Camera(ip="169.254.50.50")
        try:
            cam.connect()
            mock_gvcp_cls.assert_called_once_with("169.254.50.50", "169.254.27.140", ANY)
            mock_find.assert_not_called()
        finally:
            Camera._active_cameras.clear()

    @patch("pyTelops.camera.GVSPReceiver")
    @patch("pyTelops.camera.GVCPClient")
    @patch("pyTelops.camera._find_local_ip_for", return_value="169.254.9.9")
    @patch("pyTelops.camera.discover")
    def test_connect_explicit_ip_falls_back_when_sweep_misses(
        self, mock_disc, mock_find, mock_gvcp_cls, mock_gvsp_cls
    ):
        # A camera the sweep cannot see (routed subnet) falls back to OS
        # routing, which is correct for routed paths.
        mock_disc.return_value = []
        mock_gvcp_cls.return_value.read_reg.return_value = 0
        cam = Camera(ip="192.168.1.80")
        try:
            cam.connect()
            mock_find.assert_called_once_with("192.168.1.80")
            mock_gvcp_cls.assert_called_once_with("192.168.1.80", "169.254.9.9", ANY)
        finally:
            Camera._active_cameras.clear()

    @patch("pyTelops.camera.GVSPReceiver")
    @patch("pyTelops.camera.GVCPClient")
    @patch("pyTelops.camera._find_local_ip_for", return_value="169.254.9.9")
    @patch("pyTelops.camera.discover")
    def test_connect_explicit_ip_falls_back_when_sweep_fails(
        self, mock_disc, mock_find, mock_gvcp_cls, mock_gvsp_cls
    ):
        # A discovery error must not break explicit-IP connects.
        mock_disc.side_effect = OSError("no sockets")
        mock_gvcp_cls.return_value.read_reg.return_value = 0
        cam = Camera(ip="169.254.50.50")
        try:
            cam.connect()
            mock_find.assert_called_once_with("169.254.50.50")
        finally:
            Camera._active_cameras.clear()

    @patch("pyTelops.camera.GVSPReceiver")
    @patch("pyTelops.camera.GVCPClient")
    @patch("pyTelops.camera._find_local_ip_for", return_value="169.254.9.9")
    @patch("pyTelops.camera.discover")
    def test_connect_falls_back_when_interface_ip_is_empty(
        self, mock_disc, mock_find, mock_gvcp_cls, mock_gvsp_cls
    ):
        # The protocol layer reports an empty interface_ip when the OS chose
        # the discovery socket; connect() must treat it as unknown and fall back.
        mock_disc.return_value = [
            {
                "ip": "169.254.123.5",
                "manufacturer": "Telops Inc.",
                "model": "TS-IR",
                "reachable": True,
                "interface_ip": "",
            }
        ]
        mock_gvcp_cls.return_value.read_reg.return_value = 0
        cam = Camera()
        try:
            cam.connect()
            mock_find.assert_called_once_with("169.254.123.5")
            mock_gvcp_cls.assert_called_once_with("169.254.123.5", "169.254.9.9", ANY)
        finally:
            Camera._active_cameras.clear()


class TestDevicePowerControl:
    """Software power-state control (standby/on) and firmware reset."""

    def test_power_state_returns_enum(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = int(reg.DevicePowerState.ON)
        assert cam.power_state == reg.DevicePowerState.ON

    def test_standby_commands_setpoint_when_on(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = int(reg.DevicePowerState.ON)
        cam.standby()
        cam._gvcp.write_reg.assert_any_call(
            reg.REG_DEVICE_POWER_STATE_SETPOINT, int(reg.DevicePowerState.STANDBY)
        )

    def test_standby_idempotent_when_already_standby(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = int(reg.DevicePowerState.STANDBY)
        cam.standby()
        assert not any(
            c.args and c.args[0] == reg.REG_DEVICE_POWER_STATE_SETPOINT
            for c in cam._gvcp.write_reg.call_args_list
        )

    def test_power_on_commands_setpoint_and_waits(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = int(reg.DevicePowerState.STANDBY)
        cam.wait_until_ready = MagicMock()
        cam.power_on(timeout=300)
        cam._gvcp.write_reg.assert_any_call(
            reg.REG_DEVICE_POWER_STATE_SETPOINT, int(reg.DevicePowerState.ON)
        )
        cam.wait_until_ready.assert_called_once_with(cooling_timeout=300)

    def test_power_on_wait_false_skips_ready(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = int(reg.DevicePowerState.STANDBY)
        cam.wait_until_ready = MagicMock()
        cam.power_on(wait=False)
        cam.wait_until_ready.assert_not_called()

    def test_power_on_idempotent_when_already_on(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = int(reg.DevicePowerState.ON)
        cam.wait_until_ready = MagicMock()
        cam.power_on(wait=False)
        assert not any(
            c.args and c.args[0] == reg.REG_DEVICE_POWER_STATE_SETPOINT
            for c in cam._gvcp.write_reg.call_args_list
        )

    def test_reset_commands_reset_then_disconnects(self):
        cam = _make_fake_connected_camera()
        cam._camera_ip = "169.254.1.5"
        gvcp = cam._gvcp
        cam.reset()
        gvcp.write_reg.assert_any_call(reg.REG_DEVICE_RESET, 1)
        assert cam.is_connected is False
        # cached IP cleared so the next connect() re-discovers the rebooted camera
        assert cam.camera_ip is None

    def test_reset_requires_connection(self):
        with pytest.raises(RuntimeError):
            Camera().reset()


class TestConnectTimeout:
    @patch("pyTelops.camera.GVSPReceiver")
    @patch("pyTelops.camera.GVCPClient")
    @patch("pyTelops.camera._find_local_ip_for", return_value="169.254.9.9")
    @patch("pyTelops.camera.discover")
    def test_connect_forwards_timeouts_to_wait_until_ready(
        self, mock_disc, mock_find, mock_gvcp_cls, mock_gvsp_cls
    ):
        # issue #15: connect() must forward the readiness timeouts so a
        # from-cold camera is waited out.
        mock_disc.return_value = []
        mock_gvcp_cls.return_value.read_reg.return_value = 1  # DEVICE_NOT_READY
        mock_gvcp_cls.return_value._control_lost = False
        cam = Camera(ip="169.254.50.50")
        with patch.object(Camera, "wait_until_ready") as wur:
            try:
                cam.connect(timeout=30, cooling_timeout=900)
                wur.assert_called_once_with(timeout=30, cooling_timeout=900)
            finally:
                Camera._active_cameras.clear()


class _FakeClock:
    """Deterministic clock: each sleep() advances monotonic() by that much."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class TestWaitUntilReady:
    """Short budget when the camera is stuck/unresponsive; long budget while it
    is actively cooling or initialising (issue #15 follow-up)."""

    def test_returns_when_ready(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = 0  # DEVICE_NOT_READY = 0
        cam.wait_until_ready(verbose=False)  # returns without raising

    def test_fails_fast_when_not_progressing(self):
        # not_ready but TDC shows no cooling/init activity -> stuck -> short budget
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.side_effect = lambda a: 1 if a == reg.REG_DEVICE_NOT_READY else 0
        clk = _FakeClock()
        with (
            patch("pyTelops.camera.time.monotonic", clk.monotonic),
            patch("pyTelops.camera.time.sleep", clk.sleep),
            pytest.raises(TimeoutError),
        ):
            cam.wait_until_ready(timeout=10, cooling_timeout=600, verbose=False)
        assert 10 <= clk.t < 600  # bailed on the short budget, not the long one

    def test_patient_while_cooling(self):
        # not_ready + cooling well past the short budget, then ready -> no raise
        cam = _make_fake_connected_camera()
        state = {"n": 0}

        def rd(addr):
            if addr == reg.REG_DEVICE_NOT_READY:
                state["n"] += 1
                return 0 if state["n"] > 20 else 1
            return reg.TDC_WAITING_FOR_COOLER  # actively cooling -> progressing

        cam._gvcp.read_reg.side_effect = rd
        clk = _FakeClock()
        with (
            patch("pyTelops.camera.time.monotonic", clk.monotonic),
            patch("pyTelops.camera.time.sleep", clk.sleep),
        ):
            cam.wait_until_ready(timeout=10, cooling_timeout=600, verbose=False)
        assert clk.t > 10  # waited past the short budget because it was cooling

    def test_invalid_params_is_not_progressing(self):
        # "invalid parameters" is an error state that won't resolve by waiting
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.side_effect = lambda a: (
            1 if a == reg.REG_DEVICE_NOT_READY else reg.TDC_WAITING_FOR_VALID_PARAMS
        )
        clk = _FakeClock()
        with (
            patch("pyTelops.camera.time.monotonic", clk.monotonic),
            patch("pyTelops.camera.time.sleep", clk.sleep),
            pytest.raises(TimeoutError),
        ):
            cam.wait_until_ready(timeout=10, cooling_timeout=600, verbose=False)
        assert clk.t < 600


class TestDiagnosticsSentinel:
    """Unsupported temperature locations must map to None, not the raw ADC-floor
    sentinel the camera returns instead (issue #16)."""

    def test_temperature_sentinel_maps_to_none(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_float.return_value = -138.30128479003906
        d = cam.diagnostics()
        assert d["temperatures"]
        assert all(v is None for v in d["temperatures"].values())

    def test_real_temperature_preserved(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_float.return_value = -196.3
        d = cam.diagnostics()
        assert all(v == -196.3 for v in d["temperatures"].values())


def _make_buffer_camera(
    pre_moi: int = 100,
    frames_per_seq: int | None = None,
    fps: float = 2000.0,
    n_sequences: int = 1,
    moi_source: str = "software",
    config_kwargs: bool = True,
):
    """Fake camera configured for buffer recording.

    ``read_reg`` answers from a register map (the global 0 of
    :func:`_make_fake_connected_camera` would make buffer_record hang on the
    sequence counter), and ``read_float`` returns the frame rate.
    ``frames_per_seq`` defaults to a slot that can hold ``pre_moi``, which is
    what :meth:`buffer_configure` would enforce.
    """
    if frames_per_seq is None:
        frames_per_seq = max(400, pre_moi)
    cam = _make_fake_connected_camera()
    registers = {
        reg.REG_DEVICE_NOT_READY: 0,
        reg.REG_TDC_STATUS: 0,
        reg.REG_MEMORY_BUFFER_SEQ_SIZE: frames_per_seq,
        reg.REG_MEMORY_BUFFER_SEQ_RECORDED_SIZE: frames_per_seq,
        reg.REG_MEMORY_BUFFER_PRE_MOI_SIZE: pre_moi,
        reg.REG_MEMORY_BUFFER_MOI_SOURCE: int(reg.MemoryBufferMOISource.SOFTWARE),
        reg.REG_MEMORY_BUFFER_SEQ_COUNT: 10**6,
    }
    cam._gvcp.read_reg.side_effect = lambda addr: registers.get(addr, 0)
    cam._gvcp.read_float.return_value = fps
    cam._buffer_n_sequences = n_sequences
    if config_kwargs:
        cam._buffer_config_kwargs = dict(
            n_sequences=n_sequences,
            frames_per_seq=frames_per_seq,
            pre_moi=pre_moi,
            moi_source=moi_source,
        )
    return cam


def _written(cam):
    """Addresses written through the mock GVCP, in order."""
    return [call.args[0] for call in cam._gvcp.write_reg.call_args_list]


class TestBufferRecordWaitFor:
    """buffer_record(wait_for=...) places the software MOI at the event.

    Without it the MOI fires right after arming, so a pre_moi window holds
    only whatever the ring happened to contain.
    """

    def test_callable_runs_once_per_sequence_before_the_moi(self):
        cam = _make_buffer_camera(n_sequences=2)
        calls = []
        with patch("pyTelops.camera.time.sleep"), patch.object(Camera, "_buffer_wait_sequence"):
            cam.buffer_record(verbose=False, wait_for=lambda: calls.append(len(_written(cam))))
        assert len(calls) == 2
        order = _written(cam)
        moi_writes = [
            i for i, addr in enumerate(order) if addr == reg.REG_MEMORY_BUFFER_MOI_SOFTWARE
        ]
        assert order.index(reg.REG_ACQUISITION_ARM) < calls[0] <= moi_writes[0]
        assert moi_writes[0] < calls[1] <= moi_writes[1]

    def test_number_sleeps_once_per_sequence(self):
        cam = _make_buffer_camera(pre_moi=0, n_sequences=3)
        with (
            patch("pyTelops.camera.time.sleep") as slp,
            patch.object(Camera, "_buffer_wait_sequence"),
        ):
            cam.buffer_record(verbose=False, wait_for=2.0)
        assert [c.args[0] for c in slp.call_args_list].count(2.0) == 3

    def test_pre_moi_without_wait_for_warns_and_fills_the_window(self):
        # 2000 frames at 2000 fps need 1.0 s; the 0.5 s settle covers half.
        cam = _make_buffer_camera(pre_moi=2000, fps=2000.0)
        clk = _FakeClock()
        with (
            patch("pyTelops.camera.time.monotonic", clk.monotonic),
            patch("pyTelops.camera.time.sleep", side_effect=clk.sleep) as slp,
            patch.object(Camera, "_buffer_wait_sequence"),
            pytest.warns(UserWarning, match="wait_for"),
        ):
            cam.buffer_record(verbose=False)
        assert [c.args[0] for c in slp.call_args_list] == [0.5, 0.5, 0.3]

    def test_settle_counts_towards_the_pre_moi_window(self):
        # 100 frames at 2000 fps need 0.05 s, which the settle already covered.
        cam = _make_buffer_camera(pre_moi=100, fps=2000.0)
        clk = _FakeClock()
        with (
            patch("pyTelops.camera.time.monotonic", clk.monotonic),
            patch("pyTelops.camera.time.sleep", side_effect=clk.sleep) as slp,
            patch.object(Camera, "_buffer_wait_sequence"),
            pytest.warns(UserWarning, match="wait_for"),
        ):
            cam.buffer_record(verbose=False)
        assert [c.args[0] for c in slp.call_args_list] == [0.5, 0.3]  # settle, post-stop

    def test_later_sequences_get_no_settle_head_start(self):
        # Sequence 0 is covered by the settle; sequence 1 has to wait in full.
        cam = _make_buffer_camera(pre_moi=100, fps=2000.0, n_sequences=2)
        clk = _FakeClock()
        with (
            patch("pyTelops.camera.time.monotonic", clk.monotonic),
            patch("pyTelops.camera.time.sleep", side_effect=clk.sleep) as slp,
            patch.object(Camera, "_buffer_wait_sequence"),
            pytest.warns(UserWarning, match="wait_for"),
        ):
            cam.buffer_record(verbose=False)
        assert [c.args[0] for c in slp.call_args_list] == [0.5, 0.05, 0.3]

    def test_no_pre_moi_behaves_as_before(self):
        cam = _make_buffer_camera(pre_moi=0)
        with (
            warnings.catch_warnings(),
            patch("pyTelops.camera.time.sleep") as slp,
            patch.object(Camera, "_buffer_wait_sequence"),
        ):
            warnings.simplefilter("error")
            cam.buffer_record(verbose=False)
        assert [c.args[0] for c in slp.call_args_list] == [0.5, 0.3]  # settle, post-stop

    def test_zero_is_a_silent_escape_hatch(self):
        cam = _make_buffer_camera(pre_moi=100)
        with (
            warnings.catch_warnings(),
            patch("pyTelops.camera.time.sleep") as slp,
            patch.object(Camera, "_buffer_wait_sequence"),
        ):
            warnings.simplefilter("error")
            cam.buffer_record(verbose=False, wait_for=0)
        assert [c.args[0] for c in slp.call_args_list] == [0.5, 0.3]

    def test_too_short_wait_warns(self):
        cam = _make_buffer_camera(pre_moi=4000, fps=2000.0)  # needs 2.0 s
        with (
            patch("pyTelops.camera.time.sleep"),
            patch.object(Camera, "_buffer_wait_sequence"),
            pytest.warns(UserWarning, match="will not be full"),
        ):
            cam.buffer_record(verbose=False, wait_for=0.1)

    def test_short_wait_covered_by_the_settle_does_not_warn(self):
        # 1000 frames at 2000 fps need 0.5 s; wait_for 0.1 s plus the 0.5 s
        # settle fills the window on the only sequence, so there is nothing
        # to warn about.
        cam = _make_buffer_camera(pre_moi=1000, fps=2000.0)
        with (
            warnings.catch_warnings(),
            patch("pyTelops.camera.time.sleep"),
            patch.object(Camera, "_buffer_wait_sequence"),
        ):
            warnings.simplefilter("error")
            cam.buffer_record(verbose=False, wait_for=0.1)

    def test_short_wait_warns_when_more_sequences_follow(self):
        # Sequences after the first get no settle, so 0.1 s is short for all.
        cam = _make_buffer_camera(pre_moi=1000, fps=2000.0, n_sequences=2)
        with (
            patch("pyTelops.camera.time.sleep"),
            patch.object(Camera, "_buffer_wait_sequence"),
            pytest.warns(UserWarning, match="will not be full"),
        ):
            cam.buffer_record(verbose=False, wait_for=0.1)

    def test_quick_callable_still_fills_the_window(self):
        # 2000 frames at 2000 fps need 1.0 s; the settle spent 0.5 s of it.
        cam = _make_buffer_camera(pre_moi=2000, fps=2000.0)
        clk = _FakeClock()
        with (
            warnings.catch_warnings(),
            patch("pyTelops.camera.time.sleep", side_effect=clk.sleep) as slp,
            patch("pyTelops.camera.time.monotonic", clk.monotonic),
            patch.object(Camera, "_buffer_wait_sequence"),
        ):
            warnings.simplefilter("error")
            cam.buffer_record(verbose=False, wait_for=lambda: None)
        assert [c.args[0] for c in slp.call_args_list] == [0.5, 0.5, 0.3]

    def test_slow_callable_gets_no_top_up(self):
        cam = _make_buffer_camera(pre_moi=2000, fps=2000.0)
        clk = _FakeClock()
        with (
            warnings.catch_warnings(),
            patch("pyTelops.camera.time.sleep", side_effect=clk.sleep) as slp,
            patch("pyTelops.camera.time.monotonic", clk.monotonic),
            patch.object(Camera, "_buffer_wait_sequence"),
        ):
            warnings.simplefilter("error")
            # The callable advances the clock itself, past the 1.0 s fill time.
            cam.buffer_record(verbose=False, wait_for=lambda: clk.sleep(3.0))
        assert [c.args[0] for c in slp.call_args_list] == [0.5, 0.3]  # settle, post-stop

    def test_rejects_bad_wait_for(self):
        cam = _make_buffer_camera()
        with pytest.raises(TypeError):
            cam.buffer_record(verbose=False, wait_for="0.5")
        with pytest.raises(TypeError):
            cam.buffer_record(verbose=False, wait_for=True)
        with pytest.raises(ValueError, match=">= 0"):
            cam.buffer_record(verbose=False, wait_for=-1.0)

    def test_rejects_non_software_moi_source(self):
        cam = _make_buffer_camera(moi_source="external")
        with pytest.raises(ValueError, match="EXTERNAL_SIGNAL"):
            cam.buffer_record(verbose=False)

    def test_reads_moi_source_from_the_camera_when_unconfigured(self):
        cam = _make_buffer_camera(config_kwargs=False)
        cam._gvcp.read_reg.side_effect = lambda addr: (
            int(reg.MemoryBufferMOISource.EXTERNAL_SIGNAL)
            if addr == reg.REG_MEMORY_BUFFER_MOI_SOURCE
            else 0
        )
        with pytest.raises(ValueError, match="EXTERNAL_SIGNAL"):
            cam.buffer_record(verbose=False)

    def test_interrupt_in_wait_for_stops_acquisition(self):
        cam = _make_buffer_camera()

        def boom():
            raise KeyboardInterrupt

        with (
            patch("pyTelops.camera.time.sleep"),
            patch.object(Camera, "_buffer_wait_sequence"),
            pytest.raises(KeyboardInterrupt),
        ):
            cam.buffer_record(verbose=False, wait_for=boom)
        assert reg.REG_ACQUISITION_STOP in _written(cam)
        assert reg.REG_MEMORY_BUFFER_MOI_SOFTWARE not in _written(cam)


class TestBufferConfigurePreMOI:
    """pre_moi is a frame count inside the sequence slot."""

    def test_rejects_pre_moi_larger_than_sequence(self):
        cam = _make_fake_connected_camera()
        with pytest.raises(ValueError, match="exceeds frames_per_seq"):
            cam.buffer_configure(frames_per_seq=100, pre_moi=101)

    def test_rejects_negative_pre_moi(self):
        cam = _make_fake_connected_camera()
        with pytest.raises(ValueError, match="pre_moi must be"):
            cam.buffer_configure(frames_per_seq=100, pre_moi=-1)

    def test_full_slot_pre_moi_is_allowed(self):
        cam = _make_fake_connected_camera()
        cam.buffer_configure(frames_per_seq=100, pre_moi=100)
        cam._gvcp.write_reg.assert_any_call(reg.REG_MEMORY_BUFFER_PRE_MOI_SIZE, 100)


class TestBufferMOIPosition:
    """The camera reports where the MOI sits inside a recorded sequence."""

    def test_moi_frame_id_selects_the_sequence_first(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.side_effect = lambda addr: {
            reg.REG_MEMORY_BUFFER_SEQ_MOI_FRAME_ID: 5100,
        }.get(addr, 0)

        assert cam.buffer_moi_frame_id(sequence=2) == 5100
        cam._gvcp.write_reg.assert_called_once_with(reg.REG_MEMORY_BUFFER_SEQ_SELECTOR, 2)

    def test_moi_index_is_the_offset_from_the_first_frame(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.side_effect = lambda addr: {
            reg.REG_MEMORY_BUFFER_SEQ_MOI_FRAME_ID: 5100,
            reg.REG_MEMORY_BUFFER_SEQ_FIRST_FRAME_ID: 5000,
        }.get(addr, 0)

        assert cam.buffer_moi_index() == 100
        cam._gvcp.write_reg.assert_called_once_with(reg.REG_MEMORY_BUFFER_SEQ_SELECTOR, 0)
        read = [call.args[0] for call in cam._gvcp.read_reg.call_args_list]
        assert read == [
            reg.REG_MEMORY_BUFFER_SEQ_MOI_FRAME_ID,
            reg.REG_MEMORY_BUFFER_SEQ_FIRST_FRAME_ID,
        ]


HEADER_WIDTH = 64  # 2 rows x 64 uint16 = the 256 header bytes


def _header_frame(frame_id, posix_time, subsecond_raw, signature=SIGNATURE, height=4):
    """Build one raw uint16 frame with a Telops header in its first two rows."""
    raw = bytearray(HEADER_BYTES)
    raw[HDR_SIGNATURE : HDR_SIGNATURE + 2] = signature
    raw[HDR_XML_MINOR] = 7
    raw[HDR_XML_MAJOR] = 12
    struct.pack_into("<I", raw, HDR_FRAME_ID, frame_id)
    struct.pack_into("<I", raw, HDR_POSIX_TIME, posix_time)
    struct.pack_into("<I", raw, HDR_SUBSECOND, subsecond_raw)

    frame = np.zeros((2 + height, HEADER_WIDTH), dtype="<u2")
    frame[:2, :] = np.frombuffer(bytes(raw), dtype=np.uint8).view("<u2").reshape(2, HEADER_WIDTH)
    frame[2:, :] = frame_id
    return frame


def _cam_for_header_download(n_frames, bad_positions=(), drop_positions=()):
    """Fake camera whose _download_range hands back frames with headers.

    ``bad_positions`` get a broken signature; ``drop_positions`` never arrive,
    which is what a tolerated dropped frame looks like to buffer_download.
    """
    cam = _make_fake_connected_camera()
    cam._gvsp._resend_stats = {"requested": 0, "recovered": 0, "failed": 0}

    def fake_range(frame_id, count, **kwargs):
        out = {}
        for off in range(count):
            pos = frame_id + off
            if pos in drop_positions:
                continue
            signature = b"XX" if pos in bad_positions else SIGNATURE
            frame = _header_frame(1000 + pos, 1_757_318_400, 10_000 * pos, signature)
            out[off] = (frame, {"missing_packets": 0, "timestamp": pos})
        return out

    cam._download_range = MagicMock(side_effect=fake_range)
    return cam


class TestBufferDownloadHeaders:
    """buffer_download(return_headers=True) returns one header per frame."""

    def test_headers_align_with_the_frames(self):
        cam = _cam_for_header_download(3)
        data, headers = cam.buffer_download(
            n_frames=3, convert=False, verbose=False, return_headers=True
        )
        assert data.shape == (3, 4, HEADER_WIDTH)  # header rows stripped
        assert [h.frame_id for h in headers] == [1000, 1001, 1002]
        assert [int(data[i, 0, 0]) for i in range(3)] == [1000, 1001, 1002]
        assert headers[1].timestamp == pytest.approx(1_757_318_400 + 0.001, abs=1e-6)

    def test_headers_survive_conversion_and_stripping(self):
        cam = _cam_for_header_download(2)
        data, headers = cam.buffer_download(
            n_frames=2, convert=True, verbose=False, return_headers=True
        )
        assert data.shape[0] == len(headers) == 2
        assert [h.frame_id for h in headers] == [1000, 1001]

    def test_dropped_frame_keeps_headers_aligned(self):
        cam = _cam_for_header_download(4, drop_positions=(2,))
        data, headers = cam.buffer_download(
            n_frames=4,
            retries=0,
            max_dropped_frames=1,
            convert=False,
            verbose=False,
            return_headers=True,
        )
        assert len(headers) == data.shape[0] == 3
        assert [h.frame_id for h in headers] == [1000, 1001, 1003]
        assert [int(data[i, 0, 0]) for i in range(3)] == [1000, 1001, 1003]

    def test_headers_follow_start_frame_and_n_frames(self):
        cam = _cam_for_header_download(3)
        data, headers = cam.buffer_download(
            start_frame=5, n_frames=3, convert=False, verbose=False, return_headers=True
        )
        assert data.shape[0] == len(headers) == 3
        assert [h.frame_id for h in headers] == [1005, 1006, 1007]

    def test_bad_header_gives_none_and_one_warning(self):
        cam = _cam_for_header_download(3, bad_positions=(1,))
        with pytest.warns(UserWarning, match="1 of 3 frame headers"):
            data, headers = cam.buffer_download(
                n_frames=3, convert=False, verbose=False, return_headers=True
            )
        assert headers[1] is None
        assert [h.frame_id for h in headers if h is not None] == [1000, 1002]
        assert data.shape[0] == 3

    def test_nothing_recorded_returns_none_and_empty_list(self):
        cam = _make_fake_connected_camera()  # recorded size register reads 0
        assert cam.buffer_download(verbose=False, return_headers=True) == (None, [])

    def test_nothing_received_returns_none_and_empty_list(self):
        cam = _make_fake_connected_camera()
        cam._gvsp._resend_stats = {"requested": 0, "recovered": 0, "failed": 0}
        cam._download_range = MagicMock(return_value={})
        out = cam.buffer_download(
            n_frames=2, retries=0, max_dropped_frames=2, verbose=False, return_headers=True
        )
        assert out == (None, [])

    def test_default_still_returns_only_the_array(self):
        cam = _cam_for_header_download(2)
        out = cam.buffer_download(n_frames=2, convert=False, verbose=False)
        assert isinstance(out, np.ndarray)


class TestParseDownloadHeaders:
    """The per-frame parsing helper used by buffer_download."""

    def test_all_good(self):
        raw = np.stack([_header_frame(10 + i, 1_757_318_400, 0) for i in range(4)])
        headers = _parse_download_headers(raw)
        assert [h.frame_id for h in headers] == [10, 11, 12, 13]

    def test_counts_bad_headers_in_one_warning(self):
        raw = np.stack(
            [
                _header_frame(1, 0, 0, signature=b"XX"),
                _header_frame(2, 0, 0),
                _header_frame(3, 0, 0, signature=b"\x00\x00"),
            ]
        )
        with pytest.warns(UserWarning, match="2 of 3 frame headers"):
            headers = _parse_download_headers(raw)
        assert [h is None for h in headers] == [True, False, True]


# ============================================================
# Hardware tests (skipped without --hardware flag)
# ============================================================

# Legacy hardware tests removed — all covered by test_hardware.py
