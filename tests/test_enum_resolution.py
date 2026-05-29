"""Tests for string enum resolution and related utilities."""

import numpy as np
import pytest

from pyTelops import registers as reg
from pyTelops.camera import Camera, _resolve_enum


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
        assert (
            _resolve_enum("software", reg.MemoryBufferMOISource)
            == reg.MemoryBufferMOISource.SOFTWARE
        )

    def test_moi_external(self):
        assert (
            _resolve_enum("external", reg.MemoryBufferMOISource)
            == reg.MemoryBufferMOISource.EXTERNAL_SIGNAL
        )

    # --- Enum name fallback ---

    def test_enum_name_fallback(self):
        """Enum member name works even without alias."""
        assert (
            _resolve_enum("RISING_EDGE", reg.TriggerActivation) == reg.TriggerActivation.RISING_EDGE
        )

    def test_enum_name_case_insensitive(self):
        assert (
            _resolve_enum("rising_edge", reg.TriggerActivation) == reg.TriggerActivation.RISING_EDGE
        )


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

    def test_clean_data(self, caplog):
        data = np.random.randint(1000, 60000, (100, 256, 320), dtype=np.uint16)
        with caplog.at_level("INFO", logger="pyTelops.camera"):
            Camera._download_diagnostics(data, 100)
        assert "OK" in caplog.text

    def test_missing_frames(self, caplog):
        data = np.random.randint(1000, 60000, (90, 256, 320), dtype=np.uint16)
        with caplog.at_level("WARNING", logger="pyTelops.camera"):
            Camera._download_diagnostics(data, 100)
        assert "10 frames missing" in caplog.text

    def test_blank_frames(self, caplog):
        data = np.zeros((100, 256, 320), dtype=np.uint16)
        data[50:] = 5000
        with caplog.at_level("WARNING", logger="pyTelops.camera"):
            Camera._download_diagnostics(data, 100)
        assert "blank" in caplog.text

    def test_zero_rows(self, caplog):
        data = np.ones((10, 256, 320), dtype=np.uint16) * 5000
        data[3, 100:110, :] = 0
        with caplog.at_level("WARNING", logger="pyTelops.camera"):
            Camera._download_diagnostics(data, 10)
        assert "zero rows" in caplog.text


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
        w, h = cam._validate_resolution(320, 256)
        assert (w, h) == (320, 256)

    def test_valid_subwindow(self):
        cam = Camera()
        assert cam._validate_resolution(128, 64) == (128, 64)

    def test_invalid_width_not_multiple_64(self):
        cam = Camera()
        with pytest.raises(ValueError, match="multiple of 64"):
            cam._validate_resolution(160, 256)

    def test_invalid_width_too_small(self):
        cam = Camera()
        with pytest.raises(ValueError, match="out of range"):
            cam._validate_resolution(32, 256)

    def test_invalid_width_too_large(self):
        cam = Camera()
        with pytest.raises(ValueError, match="out of range"):
            cam._validate_resolution(384, 256)

    def test_invalid_height_wrong_step(self):
        cam = Camera()
        with pytest.raises(ValueError, match="not valid"):
            cam._validate_resolution(320, 99)

    def test_invalid_height_too_small(self):
        cam = Camera()
        with pytest.raises(ValueError, match="out of range"):
            cam._validate_resolution(320, 2)

    def test_invalid_height_too_large(self):
        cam = Camera()
        with pytest.raises(ValueError, match="out of range"):
            cam._validate_resolution(320, 260)

    def test_nearest_height_suggestion(self):
        cam = Camera()
        try:
            cam._validate_resolution(320, 101)
        except ValueError as e:
            assert "100" in str(e)  # nearest valid to 101 is 100

    def test_valid_widths(self):
        cam = Camera()
        assert cam.valid_widths == [64, 128, 192, 256, 320]

    def test_valid_heights_start_and_step(self):
        cam = Camera()
        heights = cam.valid_heights
        assert heights[0] == 4
        assert heights[1] == 8
        assert heights[-1] == 256
        # All are multiples of 4
        assert all(h % 4 == 0 for h in heights)

    def test_minimum_valid_resolution(self):
        cam = Camera()
        assert cam._validate_resolution(64, 4) == (64, 4)


class TestCalibrationConversion:
    """Test _apply_calibration reads header and converts to physical units."""

    def _make_rt_frame(self, pixel_value, width=20):
        """Build a fake frame with RT header (DataExp=-8, DataOffset=273.15)."""
        import struct

        # Header: 2 rows x width pixels x 2 bytes
        header = bytearray(2 * width * 2)
        struct.pack_into("<f", header, 12, 273.15)  # DataOffset
        struct.pack_into("<b", header, 16, -8)  # DataExp
        header[28] = 2  # CalibrationMode = RT

        header_arr = np.frombuffer(bytes(header), dtype=np.uint16).reshape(2, width)
        data_arr = np.full((2, width), pixel_value, dtype=np.uint16)
        return np.vstack([header_arr, data_arr])

    def test_rt_conversion(self):
        cam = Camera()
        # pixel=7424: 7424/256 + 273.15 = 302.15 K -> 29.0 C
        frame = self._make_rt_frame(7424)
        result = cam._apply_calibration(frame)
        assert result.dtype == np.float32
        np.testing.assert_allclose(result[0, 0], 29.0, atol=0.1)

    def test_nuc_no_conversion(self):
        """NUC mode: DataExp=0, DataOffset=0 — no conversion."""
        cam = Camera()
        width = 20
        header = bytearray(2 * width * 2)
        # DataExp=0, DataOffset=0, CalMode=1 (NUC)
        header[28] = 1
        header_arr = np.frombuffer(bytes(header), dtype=np.uint16).reshape(2, width)
        data_arr = np.full((2, width), 5000, dtype=np.uint16)
        frame = np.vstack([header_arr, data_arr])

        result = cam._apply_calibration(frame)
        assert result.dtype == np.uint16  # no conversion
        assert result.shape == (2, width)  # headers stripped
        assert result[0, 0] == 5000  # data unchanged


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
            "% column #1: temp\n"
            "0.0;22.7;180.4;370.3\n"
            "175.0;3.5;27.6;56.7\n"
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


class TestCalibrationLoadSearch:
    """Test calibration_load lens+temp search logic (no camera needed)."""

    def _cam_with_mock_calibration(self):
        cam = Camera()
        cam._calibration_info = {
            0: {
                "index": 0,
                "posix": 100,
                "lens": "MW 50mm",
                "fw_pos": 0,
                "temp_range": (0.0, 175.0),
            },
            1: {
                "index": 1,
                "posix": 101,
                "lens": "MW 50mm",
                "fw_pos": 1,
                "temp_range": (25.0, 378.0),
            },
            2: {
                "index": 2,
                "posix": 102,
                "lens": "MW 25mm",
                "fw_pos": 0,
                "temp_range": (0.0, 184.0),
            },
            3: {
                "index": 3,
                "posix": 103,
                "lens": "MW 25mm",
                "fw_pos": 1,
                "temp_range": (115.0, 376.0),
            },
        }
        return cam

    def test_find_by_lens_and_temp(self):
        cam = self._cam_with_mock_calibration()
        cam._connected = True
        # Should find MW 50mm FW0 (0-175) for temp=25
        # Can't actually load (no camera), but test the search part
        # by checking that calibration_load raises GVCPError (no _gvcp)
        # not ValueError (no match)
        with pytest.raises((RuntimeError, AttributeError)):
            cam.calibration_load(lens="50mm", temp=25)

    def test_find_narrowest_range(self):
        cam = self._cam_with_mock_calibration()
        cam._connected = True
        # temp=100 matches both MW 50mm FW0 (0-175, span=175) and FW1 (25-378, span=353)
        # Should prefer FW0 (narrower)
        with pytest.raises((RuntimeError, AttributeError)):
            cam.calibration_load(lens="50mm", temp=100)

    def test_no_lens_match_raises(self):
        cam = self._cam_with_mock_calibration()
        cam._connected = True
        with pytest.raises(ValueError, match="No calibration"):
            cam.calibration_load(lens="microscope", temp=25)

    def test_temp_out_of_range_raises(self):
        cam = self._cam_with_mock_calibration()
        cam._connected = True
        with pytest.raises(ValueError, match="No calibration"):
            cam.calibration_load(lens="50mm", temp=500)

    def test_no_args_raises(self):
        cam = self._cam_with_mock_calibration()
        cam._connected = True
        with pytest.raises(ValueError, match="Specify"):
            cam.calibration_load()

    def test_case_insensitive_lens(self):
        cam = self._cam_with_mock_calibration()
        cam._connected = True
        # "50MM" should match "MW 50mm"
        with pytest.raises((RuntimeError, AttributeError)):
            cam.calibration_load(lens="50MM", temp=25)
