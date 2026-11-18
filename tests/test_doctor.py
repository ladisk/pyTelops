"""Tests for ``pytelops doctor`` (no camera: fake sysfs/proc trees and mocks)."""

import json
from pathlib import Path
import struct
import socket
from unittest.mock import MagicMock, patch

import pytest
from pyGigEVision import GVCPError
from pyGigEVision.standard import (
    REG_CCP,
    REG_SC_DEST_ADDR,
    REG_SC_HOST_PORT,
    REG_SC_PACKET_DELAY,
    REG_SC_PACKET_SIZE,
)

import pyTelops
from pyTelops import doctor
from pyTelops import registers as reg
from pyTelops.cli import main


def test_package_is_from_worktree():
    assert (Path(pyTelops.__file__).parent / "doctor.py").exists()


def make_nic(root, name, vid=None, pid=None, driver="r8152", carrier="1", port="1-2"):
    net = root / "class" / "net" / name
    net.mkdir(parents=True)
    (net / "carrier").write_text(carrier + "\n")
    (net / "mtu").write_text("1500\n")
    if vid:
        usb = root / "devices" / "usb1" / port
        iface = usb / f"{port}:1.0"
        iface.mkdir(parents=True)
        (usb / "idVendor").write_text(vid + "\n")
        (usb / "idProduct").write_text(pid + "\n")
        (net / "device").symlink_to(iface)
        drv = root / "bus" / "drivers" / driver
        drv.mkdir(parents=True, exist_ok=True)
        (iface / "driver").symlink_to(drv)
    return net


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "linux")


class TestAdapters:
    def run(self, tmp_path, addrs=None):
        return doctor.check_adapters(str(tmp_path), host_ipv4=lambda: addrs or {})

    def test_asix_cdc_ncm_warns_with_port(self, tmp_path, linux):
        make_nic(tmp_path, "enp1", "0b95", "1790", "cdc_ncm", port="3-1")
        (res,) = self.run(tmp_path)
        assert res["status"] == "warn"
        assert "ASIX 0b95:1790 on cdc_ncm" in res["detail"]
        assert "/sys/bus/usb/devices/3-1/bConfigurationValue" in res["fix"]

    def test_asix_vendor_driver_ok(self, tmp_path, linux):
        make_nic(tmp_path, "enp1", "0b95", "1790", "ax88179_178a")
        (res,) = self.run(tmp_path)
        assert res["status"] == "ok"

    def test_tplink_warns(self, tmp_path, linux):
        make_nic(tmp_path, "enp1", "2357", "0601", "r8152")
        (res,) = self.run(tmp_path)
        assert res["status"] == "warn"
        assert "drops most stream packets" in res["fix"]

    def test_link_local_without_carrier_listed(self, tmp_path, linux):
        make_nic(tmp_path, "eth9", carrier="0")
        (res,) = self.run(tmp_path, {"eth9": ["169.254.1.2"]})
        assert res["status"] == "ok"
        assert "carrier down" in res["detail"]

    def test_no_interfaces_warns(self, tmp_path, linux):
        make_nic(tmp_path, "eth9", carrier="0")
        (res,) = self.run(tmp_path)
        assert res["status"] == "warn"

    def test_skip_off_linux(self, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor.sys, "platform", "win32")
        (res,) = self.run(tmp_path)
        assert res["status"] == "skip"


class TestRmem:
    def test_low_warns(self, tmp_path, linux):
        p = tmp_path / "rmem_max"
        p.write_text("212992\n")
        res = doctor.check_rmem(str(p))
        assert res["status"] == "warn"
        assert "sudo sysctl -w net.core.rmem_max=16777216" in res["fix"]

    def test_enough_ok(self, tmp_path, linux):
        p = tmp_path / "rmem_max"
        p.write_text("16777216\n")
        assert doctor.check_rmem(str(p))["status"] == "ok"


class TestRouteAndArp:
    def test_route_mismatch(self):
        ctx = {"ip": "169.254.1.1", "interface_ip": "169.254.1.2"}
        with patch.object(doctor, "_find_local_ip_for", return_value="10.8.0.2"):
            assert doctor.check_route(ctx)["status"] == "warn"

    def test_route_match(self):
        ctx = {"ip": "169.254.1.1", "interface_ip": "169.254.1.2"}
        with patch.object(doctor, "_find_local_ip_for", return_value="169.254.1.2"):
            assert doctor.check_route(ctx)["status"] == "ok"

    HDR = "IP address       HW type     Flags       HW address            Mask     Device\n"

    def test_arp_missing(self, tmp_path, linux):
        p = tmp_path / "arp"
        p.write_text(self.HDR)
        with patch.object(doctor, "_host_ipv4", return_value={"enp1": ["169.254.1.2"]}):
            res = doctor.check_arp({"ip": "169.254.1.1", "interface_ip": "169.254.1.2"}, str(p))
        assert res["status"] == "warn"
        assert "promisc on" in res["fix"] and "enp1" in res["fix"]

    def test_arp_incomplete(self, tmp_path, linux):
        p = tmp_path / "arp"
        p.write_text(self.HDR + "169.254.1.1 0x1 0x0 00:00:00:00:00:00 * enp1\n")
        assert doctor.check_arp({"ip": "169.254.1.1"}, str(p))["status"] == "warn"

    def test_arp_ok(self, tmp_path, linux):
        p = tmp_path / "arp"
        p.write_text(self.HDR + "169.254.1.1 0x1 0x2 aa:bb:cc:dd:ee:ff * enp1\n")
        assert doctor.check_arp({"ip": "169.254.1.1"}, str(p))["status"] == "ok"


CAM = {
    "ip": "169.254.1.1",
    "mac": "aa:bb",
    "model": "FAST M3k",
    "serial": "123",
    "device_version": "1.0",
    "interface_ip": "169.254.1.2",
    "reachable": True,
}


class TestDiscovery:
    def test_none_fails(self):
        with patch.object(doctor, "discover", return_value=[]):
            res = doctor.check_discovery({})
        assert res["status"] == "fail" and "power-cycle" in res["fix"]

    def test_unreachable_warns(self):
        with patch.object(doctor, "discover", return_value=[dict(CAM, reachable=False)]):
            res = doctor.check_discovery({})
        assert res["status"] == "warn" and "force_ip" in res["fix"]

    def test_ok_fills_ctx(self):
        ctx = {}
        with patch.object(doctor, "discover", return_value=[CAM]):
            assert doctor.check_discovery(ctx)["status"] == "ok"
        assert ctx["firmware"] == "1.0" and ctx["interface_ip"] == "169.254.1.2"

    def test_ip_not_found_keeps_ip_for_probe(self):
        ctx = {}
        with patch.object(doctor, "discover", return_value=[CAM]):
            res = doctor.check_discovery(ctx, ip="169.254.9.9")
        assert res["status"] == "fail" and ctx["ip"] == "169.254.9.9"


def ip_int(ip):
    return struct.unpack(">I", socket.inet_aton(ip))[0]


def fake_client(values, timeout_on=None):
    """Mock GVCPClient whose read_reg answers from *values*."""
    client = MagicMock()
    client._sock = MagicMock()

    def read(addr):
        if timeout_on == "all":
            raise GVCPError("Timeout waiting for ACK")
        return values.get(addr, 0)

    client.read_reg.side_effect = read
    return client


IDLE = {reg.REG_TDC_STATUS: 0, reg.REG_DEVICE_NOT_READY: 0}


class TestProbe:
    def probe(self, values, timeout_on=None, stream=False):
        client = fake_client(values, timeout_on)
        ctx = {"ip": "169.254.1.1", "interface_ip": "169.254.1.2"}
        with (
            patch.object(doctor, "GVCPClient", return_value=client) as cls,
            patch.object(doctor, "_host_ipv4", return_value={"enp1": ["169.254.1.2"]}),
            patch("pyTelops.doctor.socket.socket"),
        ):
            res = doctor.check_probe(ctx, stream)
        return res, ctx, client, cls

    def test_idle_ok_never_connects_or_writes(self):
        res, ctx, client, _ = self.probe(IDLE)
        assert res["status"] == "ok"
        client.connect.assert_not_called()
        client.write_reg.assert_not_called()
        assert ctx["ready"] is True and ctx["acquiring"] is False

    def test_acquiring_foreign_dest_warns(self):
        v = {
            reg.REG_TDC_STATUS: reg.TDC_ACQUISITION_STARTED,
            REG_SC_HOST_PORT: 5000,
            REG_SC_DEST_ADDR: ip_int("10.0.0.9"),
        }
        res, ctx, *_ = self.probe(v)
        assert res["status"] == "warn"
        assert "streaming to 10.0.0.9:5000" in res["detail"]
        assert "not an address of this host" in res["detail"]
        assert "acquisition_started" in ctx["tdc_flags"]

    def test_acquiring_own_dest(self):
        v = {
            reg.REG_TDC_STATUS: reg.TDC_ACQUISITION_STARTED,
            REG_SC_HOST_PORT: 5000,
            REG_SC_DEST_ADDR: ip_int("169.254.1.2"),
        }
        res, *_ = self.probe(v)
        assert res["status"] == "warn" and "not an address" not in res["detail"]

    def test_acquiring_with_stream_flag_not_warned(self):
        v = {reg.REG_TDC_STATUS: reg.TDC_ACQUISITION_STARTED, REG_SC_HOST_PORT: 5000}
        res, *_ = self.probe(v, stream=True)
        assert res["status"] == "ok"

    def test_not_ready_decoded(self):
        v = {reg.REG_TDC_STATUS: reg.TDC_WAITING_FOR_COOLER, reg.REG_DEVICE_NOT_READY: 1}
        res, *_ = self.probe(v)
        assert res["status"] == "warn" and "waiting_for_cooler" in res["detail"]

    def test_timeout_fails(self):
        res, *_ = self.probe({}, timeout_on="all")
        assert res["status"] == "fail" and "power-cycle" in res["fix"]

    def test_controlled_warns(self):
        v = {**IDLE, REG_CCP: 2}
        res, ctx, *_ = self.probe(v)
        assert res["status"] == "warn" and "another application" in res["detail"]
        assert ctx["controlled"] is True

    def test_no_ip_skips(self):
        assert doctor.check_probe({})["status"] == "skip"


class FakeCam:
    """Camera double with a scripted frame sequence."""

    def __init__(self, infos, connect_exc=None):
        self.infos = list(infos)
        self.connect_exc = connect_exc
        self._gvcp = MagicMock()
        self._gvcp.read_reg.side_effect = lambda a: {
            REG_SC_PACKET_SIZE: 0x5DC,
            REG_SC_PACKET_DELAY: 7,
        }[a]
        self._gvsp = MagicMock()
        self._gvsp.get_frame_with_info.side_effect = lambda timeout: (
            (None, self.infos.pop(0)) if self.infos else None
        )
        self.frame_rate = 100.0
        self.disconnect = MagicMock()
        self.acquisition_start = MagicMock()
        self.acquisition_stop = MagicMock()

    def connect(self, timeout, cooling_timeout):
        if self.connect_exc:
            raise self.connect_exc

    def sensor_temperature(self, loc):
        return 80.5


def frames(ids, complete=True):
    return [{"block_id": i, "complete": complete, "missing_packets": 0 if complete else 3} for i in ids]


def stream(cam, regs=None, n=5):
    ctx = {"ip": "169.254.1.1", "interface_ip": "169.254.1.2", "regs": regs or {}}
    with patch.object(doctor, "Camera", return_value=cam):
        return doctor.check_stream(ctx, n), ctx


class TestStream:
    def test_skip_trigger(self):
        res, _ = stream(FakeCam([]), {"trigger": 1})
        assert res["status"] == "skip" and "trigger" in res["detail"]

    def test_skip_buffer(self):
        res, _ = stream(FakeCam([]), {"buffer": 1})
        assert res["status"] == "skip" and "buffer" in res["detail"]

    def test_skip_without_probe(self):
        assert doctor.check_stream({"ip": "1.2.3.4"})["status"] == "skip"

    def test_ok(self):
        cam = FakeCam(frames(range(1, 6)))
        res, ctx = stream(cam, {"trigger": 0, "buffer": 0})
        assert res["status"] == "ok" and "5/5" in res["detail"]
        assert "bad-pixel replacement" in res["detail"]
        assert ctx["temperature_c"] == 80.5

    def test_zero_frames_fails_and_restores(self):
        cam = FakeCam([])
        res, _ = stream(cam, {"trigger": 0, "buffer": 0})
        assert res["status"] == "fail" and "power-cycle" in res["fix"]
        cam._gvcp.write_reg.assert_any_call(REG_SC_PACKET_SIZE, 0x5DC)
        cam._gvcp.write_reg.assert_any_call(REG_SC_PACKET_DELAY, 7)
        cam.disconnect.assert_called_once()

    def test_incomplete_warns(self):
        cam = FakeCam(frames(range(1, 6), complete=False))
        res, _ = stream(cam, {})
        assert res["status"] == "warn" and "5 incomplete" in res["detail"]

    def test_dropped_warns(self):
        cam = FakeCam(frames([1, 2, 5, 6, 7]))
        res, _ = stream(cam, {})
        assert res["status"] == "warn" and "2 dropped" in res["detail"]

    def test_error_midway_restores_and_disconnects(self):
        cam = FakeCam(frames([1]))
        cam._gvsp.get_frame_with_info.side_effect = RuntimeError("boom")
        res, _ = stream(cam, {})
        assert res["status"] == "fail" and "boom" in res["detail"]
        cam._gvcp.write_reg.assert_any_call(REG_SC_PACKET_DELAY, 7)
        cam.disconnect.assert_called_once()

    def test_not_ready_warns(self):
        cam = FakeCam([], connect_exc=TimeoutError("cooling"))
        res, _ = stream(cam, {})
        assert res["status"] == "warn" and "not ready" in res["detail"]
        cam.disconnect.assert_called_once()

    def test_access_denied_warns(self):
        cam = FakeCam([], connect_exc=GVCPError("x", 0x8006))
        res, _ = stream(cam, {})
        assert res["status"] == "warn" and "holds control" in res["detail"]


class TestRunAndReport:
    def test_check_exception_becomes_fail(self):
        res = doctor._safe("x", lambda: 1 / 0)
        assert res["status"] == "fail" and "ZeroDivisionError" in res["detail"]

    def test_exit_codes(self):
        mk = lambda *s: [doctor._chk("a", x) for x in s]  # noqa: E731
        assert doctor.exit_code(mk("ok", "skip")) == 0
        assert doctor.exit_code(mk("ok", "warn")) == 1
        assert doctor.exit_code(mk("warn", "fail")) == 2

    def test_format_line(self):
        line = doctor.format_line(doctor._chk("adapter e", "warn", "d", "f"))
        assert line == "[WARN] adapter e: d -> fix: f"

    def test_run_doctor_no_camera(self, tmp_path):
        rm = tmp_path / "rmem"
        rm.write_text("16777216")
        with (
            patch.object(doctor, "discover", return_value=[]),
            patch.object(doctor, "_host_ipv4", return_value={}),
        ):
            checks, camera = doctor.run_doctor(sysfs_root=str(tmp_path), rmem_path=str(rm))
        names = {c["name"]: c["status"] for c in checks}
        assert names["discovery"] == "fail" and names["probe"] == "skip"
        assert camera["ip"] is None


class TestCLI:
    def patched(self, checks, camera=None):
        return patch("pyTelops.doctor.run_doctor", return_value=(checks, camera or {"ip": None}))

    def test_json_shape_and_only_stdout(self, capsys):
        checks = [
            doctor._chk("a", "ok"),
            doctor._chk("b", "warn", "d", "f"),
            doctor._chk("c", "skip"),
        ]
        with self.patched(checks, {"ip": "1.2.3.4"}):
            code = main(["doctor", "--json"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert code == 1
        assert data["summary"] == {"ok": 1, "warn": 1, "fail": 0, "skip": 1}
        assert [p["name"] for p in data["problems"]] == ["b"]
        assert data["camera"]["ip"] == "1.2.3.4"
        assert out.count("\n") == 1

    def test_human_output_and_exit_fail(self, capsys):
        with self.patched([doctor._chk("x", "fail", "bad", "do it")]):
            assert main(["doctor"]) == 2
        assert "[FAIL] x: bad -> fix: do it" in capsys.readouterr().out

    def test_args_forwarded(self):
        with self.patched([doctor._chk("x", "ok")]) as m:
            assert main(["doctor", "--ip", "1.2.3.4", "--stream", "--frames", "7"]) == 0
        m.assert_called_once_with(ip="1.2.3.4", stream=True, frames=7)

    def test_json_silent_with_prints_inside(self, capsys):
        def noisy(**kw):
            with doctor._quiet():
                print("progress noise")
            return [doctor._chk("x", "ok")], {}

        with patch("pyTelops.doctor.run_doctor", side_effect=noisy):
            main(["doctor", "--json"])
        json.loads(capsys.readouterr().out)
