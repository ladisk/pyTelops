"""Tests for register definitions and enums."""

import pytest

from pyTelops import registers as reg


class TestRegisterAddresses:
    """Verify critical register addresses haven't drifted."""

    def test_acquisition_registers(self):
        assert reg.REG_ACQUISITION_START == 0xD314
        assert reg.REG_ACQUISITION_STOP == 0xD318
        assert reg.REG_ACQUISITION_ARM == 0xE800

    def test_exposure_registers(self):
        assert reg.REG_EXPOSURE_TIME == 0xE808
        assert reg.REG_EXPOSURE_AUTO == 0xE82C

    def test_buffer_download_mode_before_frame_id(self):
        """Download mode register exists (must be written before frame ID)."""
        assert reg.REG_MEMORY_BUFFER_DOWNLOAD_MODE == 0xE93C
        assert reg.REG_MEMORY_BUFFER_DOWNLOAD_FRAME_ID == 0xEBA8


class TestEnums:
    """Test enum definitions."""

    def test_calibration_mode_values(self):
        assert reg.CalibrationMode.RAW == 255
        assert reg.CalibrationMode.NUC == 1
        assert reg.CalibrationMode.RT == 2

    def test_exposure_auto_values(self):
        assert reg.ExposureAuto.OFF == 0
        assert reg.ExposureAuto.ONCE == 1
        assert reg.ExposureAuto.CONTINUOUS == 2

    def test_trigger_source_external(self):
        # External is 48, not 1 — this was a gotcha
        assert reg.TriggerSource.EXTERNAL_SIGNAL == 48

    def test_buffer_status_refresh(self):
        assert reg.MemoryBufferStatus.REFRESH == 255

    def test_moi_source_values(self):
        assert reg.MemoryBufferMOISource.SOFTWARE == 1
        assert reg.MemoryBufferMOISource.EXTERNAL_SIGNAL == 2


class TestRegisterInfo:
    """Test register metadata table."""

    def test_all_entries_have_three_fields(self):
        for addr, info in reg.REGISTER_INFO.items():
            assert len(info) == 3, f"Register 0x{addr:04X} has {len(info)} fields"

    def test_feature_to_address_reverse_lookup(self):
        assert reg.FEATURE_TO_ADDRESS["Width"] == reg.REG_WIDTH
        assert reg.FEATURE_TO_ADDRESS["ExposureTime"] == reg.REG_EXPOSURE_TIME

    def test_access_modes_valid(self):
        valid = {"RO", "RW", "WO"}
        for addr, info in reg.REGISTER_INFO.items():
            assert info[2] in valid, f"0x{addr:04X}: invalid access '{info[2]}'"
