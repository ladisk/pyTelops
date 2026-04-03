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


class TestResolutionValidation:
    """Test _validate_resolution and valid_widths/valid_heights."""

    def test_valid_full_frame(self):
        cam = Camera()
        w, h = cam._validate_resolution(320, 258)
        assert (w, h) == (320, 258)

    def test_valid_subwindow(self):
        cam = Camera()
        assert cam._validate_resolution(128, 66) == (128, 66)

    def test_invalid_width_not_multiple_64(self):
        cam = Camera()
        with pytest.raises(ValueError, match="multiple of 64"):
            cam._validate_resolution(160, 258)

    def test_invalid_width_too_small(self):
        cam = Camera()
        with pytest.raises(ValueError, match="out of range"):
            cam._validate_resolution(32, 258)

    def test_invalid_width_too_large(self):
        cam = Camera()
        with pytest.raises(ValueError, match="out of range"):
            cam._validate_resolution(384, 258)

    def test_invalid_height_wrong_step(self):
        cam = Camera()
        with pytest.raises(ValueError, match="not valid"):
            cam._validate_resolution(320, 100)

    def test_invalid_height_too_small(self):
        cam = Camera()
        with pytest.raises(ValueError, match="out of range"):
            cam._validate_resolution(320, 4)

    def test_invalid_height_too_large(self):
        cam = Camera()
        with pytest.raises(ValueError, match="out of range"):
            cam._validate_resolution(320, 262)

    def test_nearest_height_suggestion(self):
        cam = Camera()
        try:
            cam._validate_resolution(320, 101)
        except ValueError as e:
            assert "102" in str(e)  # nearest valid to 101 is 102

    def test_valid_widths(self):
        cam = Camera()
        assert cam.valid_widths == [64, 128, 192, 256, 320]

    def test_valid_heights_start_and_step(self):
        cam = Camera()
        heights = cam.valid_heights
        assert heights[0] == 6
        assert heights[1] == 10
        assert heights[-1] == 258
        # All satisfy (h-2) % 4 == 0
        assert all((h - 2) % 4 == 0 for h in heights)

    def test_minimum_valid_resolution(self):
        cam = Camera()
        assert cam._validate_resolution(64, 6) == (64, 6)


class TestCelsiusConversion:
    """Test _to_celsius centi-Celsius -> Celsius conversion."""

    def test_to_celsius(self):
        cam = Camera()
        arr = np.array([[2050, 7051], [10000, 500]], dtype=np.uint16)
        result = cam._to_celsius(arr)
        assert result.dtype == np.float32
        np.testing.assert_allclose(result[0, 0], 20.50, atol=0.01)
        np.testing.assert_allclose(result[0, 1], 70.51, atol=0.01)

    def test_to_celsius_3d(self):
        cam = Camera()
        arr = np.array([[[2500]]], dtype=np.uint16)
        result = cam._to_celsius(arr)
        assert result.shape == (1, 1, 1)
        np.testing.assert_allclose(result[0, 0, 0], 25.0, atol=0.01)


class TestCalibrationParsing:
    """Test load_calibration_info with mock files (no camera needed)."""

    def test_parse_old_format_filename(self, tmp_path):
        """Old format: TEL08050_TIMESTAMP_EL_MF_FW_IM_SWD.tsco"""
        (tmp_path / "TEL08050_1625592710_EL08887_MF08573_FW0_IM0_SWD0.tsco").touch()
        cam = Camera()
        cam.load_calibration_info(str(tmp_path))
        # Should parse without error; file info stored for later matching
        assert hasattr(cam, "_calibration_file_info")
        assert 1625592710 in cam._calibration_file_info

    def test_parse_new_format_filename(self, tmp_path):
        """New format: TEL08050_EL_MF_FW_IM_SWD_TIMESTAMP.tsco"""
        (tmp_path / "TEL08050_EL07938_MF08575_FW0_IM0_SWD0_1741261960.tsco").touch()
        cam = Camera()
        cam.load_calibration_info(str(tmp_path))
        assert hasattr(cam, "_calibration_file_info")
        assert 1741261960 in cam._calibration_file_info

    def test_parse_exposure_time_file(self, tmp_path):
        """Exposure time file provides lens name and temp range."""
        # Create .tsco
        (tmp_path / "TEL08050_1625592710_EL08887_MF08573_FW0_IM0_SWD0.tsco").touch()
        # Create exposure time dir and file (FW1 = FW0 in .tsco)
        et_dir = tmp_path / "estimated_ExposureTimes"
        et_dir.mkdir()
        (et_dir / "estimated_ExposureTime_ELSN08887_MF08573_FW1_IM0.txt").write_text(
            '% Camera model TEL-8050 - lens "MW 50mm" model TEL-8887 - filter wheel position #1\n'
            '% column #1: temp\n'
            '0.0;22.7;180.4;370.3\n'
            '175.0;3.5;27.6;56.7\n'
        )
        cam = Camera()
        cam.load_calibration_info(str(tmp_path))
        # Lens info should be parsed and merged into tsco records
        assert hasattr(cam, "_calibration_lens_info")
        assert len(cam._calibration_lens_info) == 1
        # Check lens name was parsed from header
        key = list(cam._calibration_lens_info.keys())[0]
        assert cam._calibration_lens_info[key]["lens"] == "MW 50mm"
        # Check temp range was parsed from data rows
        assert cam._calibration_lens_info[key]["temp_min"] == 0.0
        assert cam._calibration_lens_info[key]["temp_max"] == 175.0

    def test_nonexistent_path_raises(self):
        cam = Camera()
        with pytest.raises(FileNotFoundError):
            cam.load_calibration_info("/nonexistent/path")

    def test_empty_dir(self, tmp_path):
        cam = Camera()
        cam.load_calibration_info(str(tmp_path))
        # Should not crash; no files parsed
        assert hasattr(cam, "_calibration_file_info")
        assert len(cam._calibration_file_info) == 0

    def test_calibration_names_manual(self):
        cam = Camera()
        cam.calibration_names = {0: "MW 50mm FW0", 4: "MW 25mm FW0"}
        assert cam.calibration_names[0] == "MW 50mm FW0"
        assert cam.calibration_names[4] == "MW 25mm FW0"
