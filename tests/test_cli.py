"""Tests for CLI commands (no camera needed for most)."""

import contextlib
from unittest.mock import MagicMock, patch

import pytest

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
        mock_cam = {
            "ip": "169.254.1.1",
            "manufacturer": "Telops",
            "model": "FAST M3k",
            "serial": "123",
            "device_version": "1.0",
        }
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
        with patch("pyTelops.camera.Camera") as mock_cam_cls:
            mock_cam_cls.return_value.__enter__ = MagicMock(return_value=mock_cam)
            mock_cam_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = main(["grab"])
            assert result == 1  # failed to grab


class TestLiveSignals:
    """cmd_live turns SIGTERM/SIGHUP into SystemExit and restores handlers."""

    def _run(self, live_side_effect=None):
        import signal

        seen = {}
        mock_cam = MagicMock()

        def live(**kw):
            seen["term"] = signal.getsignal(signal.SIGTERM)
            if hasattr(signal, "SIGHUP"):
                seen["hup"] = signal.getsignal(signal.SIGHUP)
            if live_side_effect:
                raise live_side_effect

        mock_cam.live_view.side_effect = live
        with patch("pyTelops.camera.Camera") as cls:
            cls.return_value.__enter__ = MagicMock(return_value=mock_cam)
            cls.return_value.__exit__ = MagicMock(return_value=False)
            with contextlib.suppress(KeyboardInterrupt):
                main(["live"])
        return seen

    def test_handlers_installed_then_restored(self):
        import signal

        before_term = signal.getsignal(signal.SIGTERM)
        before_hup = signal.getsignal(signal.SIGHUP) if hasattr(signal, "SIGHUP") else None
        seen = self._run()
        assert seen["term"] is not before_term
        with pytest.raises(SystemExit):
            seen["term"](signal.SIGTERM, None)
        if before_hup is not None:
            assert seen["hup"] is not before_hup
            with pytest.raises(SystemExit):
                seen["hup"](signal.SIGHUP, None)
        assert signal.getsignal(signal.SIGTERM) is before_term
        if before_hup is not None:
            assert signal.getsignal(signal.SIGHUP) is before_hup

    def test_handlers_restored_after_error(self):
        import signal

        before = signal.getsignal(signal.SIGTERM)
        self._run(live_side_effect=KeyboardInterrupt())
        assert signal.getsignal(signal.SIGTERM) is before

    def test_setup_linux_mentions_usb_adapters(self, capsys):
        with patch("platform.system", return_value="Linux"):
            assert main(["setup"]) == 0
        assert "USB Ethernet" in capsys.readouterr().out


class TestDoctorCLI:
    def test_doctor_exit_code_and_line(self, capsys):
        checks = [{"name": "rmem_max", "status": "warn", "detail": "low", "fix": "sudo sysctl"}]
        with patch("pyTelops.doctor.run_doctor", return_value=(checks, {})):
            assert main(["doctor"]) == 1
        assert "[WARN] rmem_max: low -> fix: sudo sysctl" in capsys.readouterr().out


class TestMcpCommand:
    def test_parser_accepts_options(self):
        from pyTelops import cli

        with patch.object(cli, "cmd_mcp", return_value=0) as cmd:
            assert main(["mcp", "--data-dir", "/tmp/x", "--simulate"]) == 0
        args = cmd.call_args[0][0]
        assert args.data_dir == "/tmp/x" and args.simulate is True

    def test_missing_extra_gives_install_hint(self, capsys, monkeypatch):
        import sys

        for name in [
            m for m in sys.modules if m == "mcp" or m.startswith(("mcp.", "pyTelops.mcp"))
        ]:
            monkeypatch.delitem(sys.modules, name)
        monkeypatch.setitem(sys.modules, "mcp", None)  # makes "import mcp" raise ImportError
        assert main(["mcp"]) == 1
        captured = capsys.readouterr()
        assert "pip install" in captured.err and "pyTelops[mcp]" in captured.err
        assert captured.out == ""

    @pytest.mark.parametrize("missing", ["anyio", "pydantic"])
    def test_missing_dependency_of_extra_gives_install_hint(self, capsys, monkeypatch, missing):
        import sys

        for name in [m for m in sys.modules if m.startswith("pyTelops.mcp")]:
            monkeypatch.delitem(sys.modules, name)
        monkeypatch.setitem(sys.modules, missing, None)
        assert main(["mcp"]) == 1
        assert "pyTelops[mcp]" in capsys.readouterr().err
