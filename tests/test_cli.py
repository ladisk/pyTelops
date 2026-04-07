"""Tests for CLI commands (no camera needed for most)."""

import pytest
from unittest.mock import patch, MagicMock
from pyTelops.cli import main


class TestCLIParser:
    """Test argument parsing and help."""

    def test_no_args_shows_help(self, capsys):
        result = main([])
        assert result == 0

    def test_version(self, capsys):
        with pytest.raises(SystemExit, match="0"):
            main(["--version"])

    def test_discover_command(self):
        with patch("pyTelops.camera.discover", return_value=[]):
            result = main(["discover"])
            assert result == 1  # no cameras found

    def test_discover_finds_camera(self, capsys):
        mock_cam = {"ip": "169.254.1.1", "manufacturer": "Telops",
                    "model": "FAST M3k", "serial": "123",
                    "device_version": "1.0"}
        with patch("pyTelops.camera.discover", return_value=[mock_cam]):
            result = main(["discover"])
            assert result == 0
            output = capsys.readouterr().out
            assert "Telops" in output
            assert "169.254.1.1" in output

    def test_setup_windows(self, capsys):
        with patch("platform.system", return_value="Windows"):
            result = main(["setup"])
            assert result == 0
            output = capsys.readouterr().out
            assert "firewall" in output.lower() or "Firewall" in output

    def test_setup_linux(self, capsys):
        with patch("platform.system", return_value="Linux"):
            result = main(["setup"])
            assert result == 0
            output = capsys.readouterr().out
            assert "rmem" in output or "UDP" in output

    def test_grab_no_camera(self):
        mock_cam = MagicMock()
        mock_cam.grab.return_value = None
        with patch("pyTelops.camera.Camera") as MockCam:
            MockCam.return_value.__enter__ = MagicMock(return_value=mock_cam)
            MockCam.return_value.__exit__ = MagicMock(return_value=False)
            result = main(["grab"])
            assert result == 1  # failed to grab
