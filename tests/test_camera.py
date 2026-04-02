"""Tests for Camera class.

Unit tests use mocking. Hardware tests require --hardware flag.
"""

import pytest
from unittest.mock import MagicMock, patch

from pyTelops.camera import Camera, discover, _find_link_local_ip


class TestCameraInit:
    """Test Camera construction (no network)."""

    def test_default_init(self):
        cam = Camera()
        assert cam._camera_ip is None
        assert not cam.is_connected
        assert not cam.is_streaming

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

@pytest.mark.hardware
class TestCameraHardware:
    """Tests that require a connected Telops camera."""

    def test_connect_disconnect(self):
        cam = Camera()
        cam.connect()
        assert cam.is_connected
        cam.disconnect()
        assert not cam.is_connected

    def test_info(self):
        with Camera() as cam:
            info = cam.info
            assert "width" in info
            assert "height" in info
            assert info["width"] > 0

    def test_grab_single_frame(self):
        with Camera() as cam:
            frame = cam.grab()
            assert frame is not None
            assert frame.ndim == 2
            assert frame.dtype.kind == "u"  # unsigned int

    def test_integration_time_property(self):
        with Camera() as cam:
            original = cam.integration_time
            cam.integration_time = 100.0
            assert abs(cam.integration_time - 100.0) < 1.0
            cam.integration_time = original

    def test_frame_rate_property(self):
        with Camera() as cam:
            original = cam.frame_rate
            cam.frame_rate = 50.0
            assert abs(cam.frame_rate - 50.0) < 1.0
            cam.frame_rate = original
