"""
Hardware test suite for pyTelops.

Requires a connected Telops camera. Run with:
    pytest tests/test_hardware.py --hardware -v

Uses a single shared Camera instance (module scope) for most tests.
Connection lifecycle tests run first and manage their own instances.
Total runtime: ~2-3 minutes depending on buffer sizes.
"""

import time
import numpy as np
import pytest

from pyTelops import Camera, discover
from pyTelops import registers as reg
from pyTelops.gvcp import GVCPError


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture(scope="module")
def cam():
    """Single camera instance shared across all tests in this module."""
    camera = Camera()
    camera.connect()
    yield camera
    camera.disconnect()


# ============================================================
# 1. Discovery & Connection (uses cam fixture — single connection)
# ============================================================

@pytest.mark.hardware
class TestDiscovery:

    def test_discover_finds_camera(self):
        cameras = discover(timeout=3.0)
        assert len(cameras) > 0
        assert cameras[0]["ip"]
        assert cameras[0]["manufacturer"]

    def test_connected(self, cam):
        assert cam.is_connected
        assert cam.state in ("connected", "standby")

    def test_reconnect_same_process(self, cam):
        """New Camera auto-disconnects stale instance via registry."""
        ip = cam.camera_ip
        c2 = Camera(ip=ip)
        c2.connect()
        assert c2.is_connected
        # Return control to the fixture's cam by reconnecting it
        c2.disconnect()
        cam.connect()


# ============================================================
# 2. Camera Info & Properties
# ============================================================

@pytest.mark.hardware
class TestProperties:

    def test_info(self, cam):
        info = cam.info
        assert info["width"] > 0
        assert info["height"] > 0
        assert "integration_time_us" in info
        assert "frame_rate_hz" in info
        assert "frame_rate_max_hz" in info
        assert "calibration" in info

    def test_state(self, cam):
        assert cam.state in ("connected", "standby")

    def test_resolution(self, cam):
        w, h = cam.resolution
        assert w > 0 and h > 0

    def test_integration_time_read_write(self, cam):
        orig = cam.integration_time
        cam.integration_time = 100.0
        assert abs(cam.integration_time - 100.0) < 2.0
        cam.integration_time = orig

    def test_frame_rate_read_write(self, cam):
        orig = cam.frame_rate
        cam.frame_rate = 200.0
        assert abs(cam.frame_rate - 200.0) < 5.0
        cam.frame_rate = orig

    def test_frame_rate_max(self, cam):
        max_fps = cam.frame_rate_max
        assert max_fps > 0

    def test_frame_rate_clamped(self, cam):
        """Setting fps above max should clamp and warn."""
        import warnings
        max_fps = cam.frame_rate_max
        orig = cam.frame_rate

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            cam.frame_rate = max_fps + 1000
            assert len(w) == 1
            assert "exceeds max" in str(w[0].message)

        actual = cam.frame_rate
        assert actual <= max_fps + 1
        cam.frame_rate = orig

    def test_temperature(self, cam):
        temp = cam.temperature
        assert isinstance(temp, float)

    def test_calibration_mode_string(self, cam):
        """String enum works for calibration mode."""
        cam.buffer_clear()
        orig = cam.calibration_mode

        cam.calibration_mode = "NUC"
        assert cam.calibration_mode == reg.CalibrationMode.NUC

        cam.calibration_mode = "RT"
        assert cam.calibration_mode == reg.CalibrationMode.RT

        cam.calibration_mode = orig

    def test_integration_time_auto_string(self, cam):
        orig = cam.integration_time_auto
        cam.integration_time_auto = "off"
        assert cam.integration_time_auto == reg.ExposureAuto.OFF
        cam.integration_time_auto = orig


# ============================================================
# 3. Live Streaming
# ============================================================

@pytest.mark.hardware
class TestStreaming:

    def test_grab_single_frame(self, cam):
        frame = cam.grab()
        assert frame is not None
        assert frame.ndim == 2
        # RT mode: float32 Celsius; other modes: uint16
        assert frame.dtype in (np.float32, np.uint16)
        w, h = cam.resolution
        assert frame.shape == (h - cam.HEADER_ROWS, w)

    def test_grab_raw_with_headers(self, cam):
        frame = cam.grab(strip_header=False)
        assert frame is not None
        w, h = cam.resolution
        assert frame.shape == (h, w)

    def test_grab_has_real_data(self, cam):
        frame = cam.grab()
        assert frame.std() > 0, "Frame is constant — likely no real data"
        assert frame.max() > 0, "Frame is all zeros"

    def test_acquire_multiple(self, cam):
        frames = cam.acquire(5, timeout=10.0)
        assert frames is not None
        assert frames.shape[0] >= 1  # at least 1 frame
        assert frames.ndim == 3

    def test_acquire_stripped(self, cam):
        frames = cam.acquire(3, strip_header=True)
        w, h = cam.resolution
        assert frames.shape[1] == h - cam.HEADER_ROWS

    def test_stream_start_stop_restart(self, cam):
        cam.start_stream()
        assert cam.is_streaming
        assert cam.state == "streaming"

        cam._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)
        result = cam._gvsp.get_frame_with_info(timeout=5.0)
        assert result is not None

        cam.stop_stream()
        assert not cam.is_streaming
        assert cam.state in ("connected", "standby")

        # Restart
        cam.start_stream()
        assert cam.is_streaming
        cam._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)
        result = cam._gvsp.get_frame_with_info(timeout=5.0)
        assert result is not None
        cam.stop_stream()


# ============================================================
# 4. Buffer Recording
# ============================================================

@pytest.fixture(autouse=True)
def reset_buffer(cam, request):
    """Reset buffer state before each buffer/workflow test."""
    if request.node.parent and request.node.parent.name in (
            "TestBuffer", "TestFullWorkflow"):
        try:
            cam._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)
        except (GVCPError, AttributeError):
            pass
        try:
            cam._gvcp.write_reg(reg.REG_MEMORY_BUFFER_MODE, 0)
        except (GVCPError, AttributeError):
            pass
        time.sleep(0.3)


@pytest.mark.hardware
class TestBuffer:

    def test_buffer_configure_with_frames(self, cam):
        cam.buffer_configure(n_sequences=1, frames_per_seq=50,
                             moi_source="software")
        info = cam.buffer_info()
        assert info["n_sequences"] == 1
        cam.buffer_clear()

    def test_buffer_configure_with_duration(self, cam):
        orig_fps = cam.frame_rate
        cam.frame_rate = 1000.0
        cam.buffer_configure(n_sequences=1, duration=0.1,
                             moi_source="software")
        info = cam.buffer_info()
        assert info["n_sequences"] == 1
        cam.buffer_clear()
        cam.frame_rate = orig_fps

    def test_buffer_configure_duration_and_frames_raises(self, cam):
        with pytest.raises(ValueError, match="not both"):
            cam.buffer_configure(duration=5.0, frames_per_seq=1000)

    def test_buffer_record_and_info(self, cam):
        cam.integration_time = 30.0
        cam.frame_rate = 2000.0
        cam.buffer_configure(n_sequences=1, frames_per_seq=50,
                             moi_source="software")

        recorded = cam.buffer_record(verbose=False)
        assert recorded == 50

        info = cam.buffer_info()
        assert info["recorded"][0] == 50
        cam.buffer_clear()

    def test_buffer_record_multiple_sequences(self, cam):
        """buffer_record() handles multi-sequence automatically."""
        cam.integration_time = 30.0
        cam.frame_rate = 2000.0
        cam.buffer_configure(n_sequences=3, frames_per_seq=50,
                             moi_source="software")

        total = cam.buffer_record(verbose=False)
        assert total == 150  # 3 x 50

        info = cam.buffer_info()
        assert all(r == 50 for r in info["recorded"])

        cam.buffer_clear()

    def test_buffer_multi_sequence_manual(self, cam):
        """Manual multi-sequence: arm + fire_moi + poll seq count."""
        cam.integration_time = 30.0
        cam.frame_rate = 2000.0
        cam.buffer_configure(n_sequences=3, frames_per_seq=50,
                             moi_source="software")

        cam.buffer_arm()
        time.sleep(1.0)

        for i in range(3):
            cam.buffer_fire_moi()
            # Poll sequence count register until this seq completes
            cam._buffer_wait_sequence(i + 1, timeout=30.0)

        try:
            cam._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)
        except GVCPError:
            pass
        time.sleep(0.3)

        info = cam.buffer_info()
        assert all(r == 50 for r in info["recorded"])

        cam.buffer_clear()

    def test_buffer_download(self, cam):
        cam.integration_time = 30.0
        cam.frame_rate = 2000.0
        cam.buffer_configure(n_sequences=1, frames_per_seq=50,
                             moi_source="software")
        cam.buffer_record(verbose=False)

        data = cam.buffer_download(sequence=0, verbose=False)
        assert data is not None
        assert data.shape[0] >= 30  # allow frame loss in download
        assert data.ndim == 3

        # Should be stripped
        w, h = cam.resolution
        assert data.shape[1] == h - cam.HEADER_ROWS

        cam.buffer_clear()

    def test_buffer_download_raw(self, cam):
        cam.frame_rate = 1000.0
        cam.buffer_configure(n_sequences=1, frames_per_seq=20,
                             moi_source="software")
        cam.buffer_record(verbose=False)

        data = cam.buffer_download(sequence=0, strip_header=False,
                                   verbose=False)
        w, h = cam.resolution
        assert data.shape[1] == h  # not stripped

        cam.buffer_clear()

    def test_buffer_download_data_integrity(self, cam):
        """Downloaded data should have real thermal content."""
        cam.frame_rate = 1000.0
        cam.buffer_configure(n_sequences=1, frames_per_seq=50,
                             moi_source="software")
        cam.buffer_record(verbose=False)

        data = cam.buffer_download(sequence=0, verbose=False)

        # Not all zeros
        assert data.max() > 0
        # Not constant
        assert data.std() > 0
        # No blank frames
        frame_means = data.mean(axis=(1, 2))
        assert np.all(frame_means > 0)

        cam.buffer_clear()

    def test_buffer_wait_timeout(self, cam):
        """buffer_wait should raise TimeoutError."""
        cam.integration_time = 30.0
        cam.frame_rate = 100.0
        cam.buffer_configure(n_sequences=1, frames_per_seq=10000,
                             moi_source="software")

        cam.buffer_arm()
        time.sleep(0.5)  # let camera enter RECORDING state
        cam.buffer_fire_moi()

        with pytest.raises(TimeoutError):
            cam.buffer_wait(timeout=3.0)

        # Clean up
        try:
            cam._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)
        except GVCPError:
            pass
        cam.buffer_clear()

    def test_buffer_status(self, cam):
        status = cam.buffer_status()
        assert isinstance(status, reg.MemoryBufferStatus)

    def test_buffer_info_has_space(self, cam):
        info = cam.buffer_info()
        assert "total_bytes" in info
        assert "free_bytes" in info
        assert info["total_bytes"] > 0


# ============================================================
# 5. Calibration
# ============================================================

@pytest.mark.hardware
class TestCalibration:

    def test_calibration_collections_returns_list(self, cam):
        colls = cam.calibration_collections()
        assert isinstance(colls, list)
        assert len(colls) > 0
        assert "index" in colls[0]
        assert "posix" in colls[0]

    def test_calibration_load_by_index(self, cam):
        cam.calibration_load(index=0)
        active = cam.calibration_active()
        assert active is not None
        assert "collection_posix" in active

    def test_calibration_load_same_twice_no_error(self, cam):
        cam.calibration_load(index=0)
        cam.calibration_load(index=0)  # should not crash

    def test_calibration_active(self, cam):
        active = cam.calibration_active()
        assert "type" in active
        assert "collection_posix" in active


# ============================================================
# 6. New Properties
# ============================================================

@pytest.mark.hardware
class TestNewProperties:

    def test_bad_pixel_replacement(self, cam):
        orig = cam.bad_pixel_replacement
        cam.bad_pixel_replacement = not orig
        assert cam.bad_pixel_replacement != orig
        cam.bad_pixel_replacement = orig

    def test_reverse_x(self, cam):
        orig = cam.reverse_x
        cam.reverse_x = not orig
        assert cam.reverse_x != orig
        cam.reverse_x = orig

    def test_reverse_y(self, cam):
        orig = cam.reverse_y
        cam.reverse_y = not orig
        assert cam.reverse_y != orig
        cam.reverse_y = orig

    def test_test_image(self, cam):
        orig = cam.test_image
        # Don't actually set to a test image (messes up streaming)
        assert orig.name == "OFF" or orig.value == 0

    def test_frame_rate_mode(self, cam):
        orig = cam.frame_rate_mode
        assert orig is not None

    def test_trigger_frame_count(self, cam):
        orig = cam.trigger_frame_count
        assert isinstance(orig, int)

    def test_roi_offset(self, cam):
        x, y = cam.roi_offset
        assert isinstance(x, int)
        assert isinstance(y, int)

    def test_valid_widths(self, cam):
        assert cam.valid_widths == [64, 128, 192, 256, 320]

    def test_valid_heights(self, cam):
        heights = cam.valid_heights
        assert heights[0] == 6
        assert heights[-1] == 258
        assert all((h - 2) % 4 == 0 for h in heights)


# ============================================================
# 7. Resolution Changes
# ============================================================

@pytest.mark.hardware
class TestResolution:

    def test_change_resolution_and_grab(self, cam):
        orig = cam.resolution
        cam.resolution = (128, 130)
        time.sleep(1.0)
        frame = cam.grab(convert=False)
        assert frame is not None
        assert frame.shape == (128, 128)  # 130 - 2 header rows
        cam.resolution = orig
        time.sleep(1.0)

    def test_invalid_width_raises(self, cam):
        with pytest.raises(ValueError, match="multiple of 64"):
            cam.resolution = (160, 258)

    def test_invalid_height_raises(self, cam):
        with pytest.raises(ValueError, match="not valid"):
            cam.resolution = (320, 100)


# ============================================================
# 8. RT Conversion
# ============================================================

@pytest.mark.hardware
class TestRTConversion:

    def test_grab_rt_returns_celsius(self, cam):
        cam.calibration_mode = "RT"
        frame = cam.grab()
        assert frame.dtype == np.float32
        # Scene should be roughly -50 to 200 C
        mean = frame[frame > 0].mean()
        assert -50 < mean < 200, f"Mean {mean} C not in expected range"

    def test_grab_convert_false_returns_uint16(self, cam):
        cam.calibration_mode = "RT"
        frame = cam.grab(convert=False)
        assert frame.dtype == np.uint16

    def test_grab_nuc_no_conversion(self, cam):
        cam.calibration_mode = "NUC"
        frame = cam.grab()
        assert frame.dtype == np.uint16  # no conversion in NUC mode
        cam.calibration_mode = "RT"


# ============================================================
# 9. Diagnostics (Hardware)
# ============================================================

@pytest.mark.hardware
class TestDiagnosticsHW:

    def test_sensor_temperature(self, cam):
        temp = cam.sensor_temperature("sensor")
        assert isinstance(temp, float)

    def test_diagnostics_returns_dict(self, cam):
        d = cam.diagnostics()
        assert "temperatures" in d
        assert "voltages" in d
        assert "currents" in d
        assert "device_running_s" in d

    def test_posix_time(self, cam):
        import datetime
        dt = cam.posix_time
        assert isinstance(dt, datetime.datetime)

    def test_gev_timestamp(self, cam):
        ts = cam.gev_timestamp_ns
        assert isinstance(ts, int)
        assert ts > 0

    def test_tdc_status(self, cam):
        tdc = cam.tdc_status
        assert isinstance(tdc, int)


# ============================================================
# 10. Full Workflow (end-to-end)
# ============================================================

@pytest.mark.hardware
class TestFullWorkflow:

    def test_complete_measurement(self, cam):
        """Full workflow: configure → record → download → verify."""
        # Configure
        cam.integration_time = 30.0
        cam.frame_rate = 2000.0
        cam.calibration_mode = "RT"

        # Buffer: 100 frames
        cam.buffer_configure(n_sequences=1, frames_per_seq=100,
                             moi_source="software")

        # Record
        recorded = cam.buffer_record(verbose=False)
        assert recorded > 0

        # Download
        data = cam.buffer_download(sequence=0, verbose=False)
        assert data is not None
        assert data.shape[0] >= recorded // 2  # allow download frame loss
        assert data.ndim == 3

        # Verify data quality
        assert data.max() > 0
        assert data.std() > 0
        frame_means = data.mean(axis=(1, 2))
        assert np.all(frame_means > 0)

        # Clean up
        cam.buffer_clear()

    def test_multiple_recordings_selective_download(self, cam):
        """Record 3 sequences, download only the second one."""
        cam.integration_time = 30.0
        cam.frame_rate = 2000.0
        cam.buffer_configure(n_sequences=3, frames_per_seq=30,
                             moi_source="software")

        total = cam.buffer_record(verbose=False)
        assert total == 90  # 3 x 30

        info = cam.buffer_info()
        assert all(r == 30 for r in info["recorded"])

        # Download only sequence 1
        data = cam.buffer_download(sequence=1, verbose=False)
        assert data is not None
        assert data.shape[0] >= 28  # allow minor loss

        cam.buffer_clear()
