"""Tests for string enum resolution and related utilities."""

import numpy as np
import pytest

from pyTelops.camera import _resolve_enum, Camera
from pyTelops import registers as reg


class TestResolveEnum:
    """Test _resolve_enum helper."""

    # --- CalibrationMode ---

    def test_string_rt(self):
        assert _resolve_enum("RT", reg.CalibrationMode) == reg.CalibrationMode.RT

    def test_string_rt_lowercase(self):
        assert _resolve_enum("rt", reg.CalibrationMode) == reg.CalibrationMode.RT

    def test_string_rt_whitespace(self):
        assert _resolve_enum("  rt  ", reg.CalibrationMode) == reg.CalibrationMode.RT

    def test_string_nuc(self):
        assert _resolve_enum("nuc", reg.CalibrationMode) == reg.CalibrationMode.NUC

    def test_string_raw(self):
        assert _resolve_enum("raw", reg.CalibrationMode) == reg.CalibrationMode.RAW

    def test_enum_passthrough(self):
        assert _resolve_enum(reg.CalibrationMode.RT, reg.CalibrationMode) == reg.CalibrationMode.RT

    def test_int_value(self):
        assert _resolve_enum(2, reg.CalibrationMode) == reg.CalibrationMode.RT

    def test_invalid_string(self):
        with pytest.raises(ValueError, match="Unknown CalibrationMode"):
            _resolve_enum("nonexistent", reg.CalibrationMode)

    def test_invalid_type(self):
        with pytest.raises(TypeError, match="Expected CalibrationMode"):
            _resolve_enum(3.14, reg.CalibrationMode)

    # --- ExposureAuto (integration_time_auto) ---

    def test_integration_time_auto_off(self):
        assert _resolve_enum("off", reg.ExposureAuto) == reg.ExposureAuto.OFF

    def test_integration_time_auto_continuous(self):
        assert _resolve_enum("continuous", reg.ExposureAuto) == reg.ExposureAuto.CONTINUOUS

    def test_integration_time_auto_once(self):
        assert _resolve_enum("once", reg.ExposureAuto) == reg.ExposureAuto.ONCE

    # --- TriggerSource ---

    def test_trigger_software(self):
        assert _resolve_enum("software", reg.TriggerSource) == reg.TriggerSource.SOFTWARE

    def test_trigger_external(self):
        assert _resolve_enum("external", reg.TriggerSource) == reg.TriggerSource.EXTERNAL_SIGNAL

    # --- TriggerActivation ---

    def test_trigger_rising(self):
        assert _resolve_enum("rising", reg.TriggerActivation) == reg.TriggerActivation.RISING_EDGE

    def test_trigger_falling(self):
        assert _resolve_enum("falling", reg.TriggerActivation) == reg.TriggerActivation.FALLING_EDGE

    # --- MemoryBufferMOISource ---

    def test_moi_software(self):
        assert _resolve_enum("software", reg.MemoryBufferMOISource) == reg.MemoryBufferMOISource.SOFTWARE

    def test_moi_external(self):
        assert _resolve_enum("external", reg.MemoryBufferMOISource) == reg.MemoryBufferMOISource.EXTERNAL_SIGNAL

    # --- Enum name fallback ---

    def test_enum_name_fallback(self):
        """Enum member name works even without alias."""
        assert _resolve_enum("RISING_EDGE", reg.TriggerActivation) == reg.TriggerActivation.RISING_EDGE

    def test_enum_name_case_insensitive(self):
        assert _resolve_enum("rising_edge", reg.TriggerActivation) == reg.TriggerActivation.RISING_EDGE


class TestStripHeaders:
    """Test _strip_headers helper."""

    def test_strip_2d(self):
        cam = Camera()
        arr = np.zeros((258, 320), dtype=np.uint16)
        result = cam._strip_headers(arr)
        assert result.shape == (256, 320)

    def test_strip_3d(self):
        cam = Camera()
        arr = np.zeros((10, 258, 320), dtype=np.uint16)
        result = cam._strip_headers(arr)
        assert result.shape == (10, 256, 320)

    def test_strip_preserves_data(self):
        cam = Camera()
        arr = np.arange(258 * 320, dtype=np.uint16).reshape(258, 320)
        result = cam._strip_headers(arr)
        # First row of result should be row 2 of original
        np.testing.assert_array_equal(result[0], arr[2])

    def test_no_strip_when_zero_headers(self):
        cam = Camera()
        cam.HEADER_ROWS = 0
        arr = np.zeros((258, 320), dtype=np.uint16)
        result = cam._strip_headers(arr)
        assert result.shape == (258, 320)


class TestDownloadDiagnostics:
    """Test _download_diagnostics static method."""

    def test_clean_data(self, capsys):
        data = np.random.randint(1000, 60000, (100, 256, 320), dtype=np.uint16)
        Camera._download_diagnostics(data, 100)
        output = capsys.readouterr().out
        assert "OK" in output

    def test_missing_frames(self, capsys):
        data = np.random.randint(1000, 60000, (90, 256, 320), dtype=np.uint16)
        Camera._download_diagnostics(data, 100)
        output = capsys.readouterr().out
        assert "WARNING" in output
        assert "10 frames missing" in output

    def test_blank_frames(self, capsys):
        data = np.zeros((100, 256, 320), dtype=np.uint16)
        data[50:] = 5000  # only first 50 are blank
        Camera._download_diagnostics(data, 100)
        output = capsys.readouterr().out
        assert "WARNING" in output
        assert "blank" in output

    def test_zero_rows(self, capsys):
        data = np.ones((10, 256, 320), dtype=np.uint16) * 5000
        data[3, 100:110, :] = 0  # zero band in frame 3
        Camera._download_diagnostics(data, 10)
        output = capsys.readouterr().out
        assert "WARNING" in output
        assert "zero rows" in output


class TestBufferConfigureDuration:
    """Test duration parameter logic (no camera needed — just validation)."""

    def test_duration_and_frames_raises(self):
        cam = Camera()
        cam._connected = True  # fake connected
        with pytest.raises(ValueError, match="not both"):
            cam.buffer_configure(duration=5.0, frames_per_seq=1000)

    def test_neither_uses_default(self):
        """When neither duration nor frames_per_seq given, default is 100."""
        # Can't fully test without camera, but verify the parameter logic
        cam = Camera()
        # Not connected, will raise RuntimeError before reaching register writes
        with pytest.raises(RuntimeError):
            cam.buffer_configure()


class TestCameraInit:
    """Test Camera construction edge cases."""

    def test_default_buffer_tracking(self):
        cam = Camera()
        assert cam._buffer_n_sequences == 1
        assert cam._buffer_next_sequence == 0

    def test_del_on_disconnected(self):
        """__del__ should not crash on a never-connected Camera."""
        cam = Camera()
        del cam  # should not raise

    def test_repr(self):
        cam = Camera(ip="1.2.3.4")
        assert "1.2.3.4" in repr(cam)
        assert "disconnected" in repr(cam)
