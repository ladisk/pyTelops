"""MCP server tests: SDK client in-process against the simulated camera."""

import inspect
import json
import sys
import threading
import time

import numpy as np
import pytest

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402
from pyGigEVision import GVCPError  # noqa: E402

from pyTelops import (
    CalibrationMode,  # noqa: E402
    Camera,  # noqa: E402
)
from pyTelops.mcp import session as session_mod  # noqa: E402
from pyTelops.mcp.server import build_server  # noqa: E402
from pyTelops.mcp.session import explain  # noqa: E402
from pyTelops.mcp.sim import SimCamera, SimWorld  # noqa: E402

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def fake_doctor(ip=None, stream=False, frames=20):
    checks = [
        {"name": "adapters", "status": "ok", "detail": "fine", "fix": ""},
        {"name": "rmem_max", "status": "warn", "detail": "small", "fix": "sysctl"},
    ]
    return checks, {"ip": "192.0.2.10"}


class Rig:
    """Server, session and simulated world for one test."""

    def __init__(self, tmp_path, cooldown_s=0.0, grace=5.0, **cam_kwargs):
        self.world = SimWorld(cooldown_s=cooldown_s)
        self.cams = []

        def factory(ip):
            cam = SimCamera(ip=ip, world=self.world, **cam_kwargs)
            self.cams.append(cam)
            return cam

        self.data = tmp_path / "data"
        self.server, self.session = build_server(
            self.data, factory, simulated=True, doctor_fn=fake_doctor, shutdown_grace_s=grace
        )
        self.session.connect_wait_s = 3.0

    @property
    def calls(self):
        return self.world.calls


async def call(client, tool, /, **args):
    return await client.call_tool(tool, args)


async def job_wait(client, job_id, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r = await call(client, "job_status", job_id=job_id)
        if r.structured_content["state"] != "running":
            return r.structured_content
        await sleep(0.05)
    raise AssertionError("job did not finish")


async def sleep(s):
    import anyio

    await anyio.sleep(s)


async def connect(client):
    r = await call(client, "camera_connect")
    assert not r.is_error and r.structured_content["connected"]


def text(r):
    return r.content[0].text


# ----- tools and annotations --------------------------------------------------------------
async def test_tool_list_and_annotations(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {
        "camera_doctor", "camera_connect", "camera_disconnect", "camera_status",
        "camera_configure", "camera_grab", "buffer_record", "buffer_download",
        "buffer_clear", "camera_nuc", "job_status", "job_cancel",
    }  # fmt: skip
    for t in tools.values():
        assert t.annotations.open_world_hint is False, t.name
    for name in ("camera_status", "job_status"):
        assert tools[name].annotations.read_only_hint is True
    for name in ("camera_connect", "camera_disconnect", "camera_configure"):
        assert tools[name].annotations.idempotent_hint is True
    assert tools["buffer_clear"].annotations.destructive_hint is True
    assert tools["camera_doctor"].annotations.read_only_hint is False
    for name in ("camera_grab", "buffer_record", "buffer_download", "camera_nuc"):
        assert tools[name].annotations.destructive_hint is False
    assert tools["camera_grab"].output_schema is not None


# ----- connect ------------------------------------------------------------------------------
async def test_connect_immediate_and_status(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        r = await call(c, "camera_status")
        assert r.structured_content["connected"] is False
        r = await call(c, "camera_connect")
        sc = r.structured_content
        assert sc["connected"] and sc["job"]["state"] == "done"
        assert sc["settings"]["width"] == 320
        assert rig.cams[0].defaults_applied
        r = await call(c, "camera_status")
        sc = r.structured_content
        assert sc["connected"] and sc["live"] and sc["settings"]["calibration"] == "RT"
        r = await call(c, "camera_connect")
        assert text(r) == "Already connected."
        r = await call(c, "camera_disconnect")
        assert not r.is_error and not rig.session.connected
        r = await call(c, "camera_status")
        assert r.structured_content["connected"] is False
        assert r.structured_content["settings"] is None


async def test_connect_cooldown_runs_as_job(tmp_path, monkeypatch):
    monkeypatch.setattr(session_mod, "COOL_SLICE_S", 0.2)
    rig = Rig(tmp_path, cooldown_s=1.0)
    rig.session.connect_wait_s = 0.1
    async with Client(rig.server) as c:
        r = await call(c, "camera_connect")
        sc = r.structured_content
        assert sc["connected"] is False and sc["job"]["state"] == "running"
        done = await job_wait(c, sc["job"]["job_id"])
        assert done["state"] == "done", done
        assert rig.session.connected
        # connected twice: first attempt times out cooling, second applies the defaults
        assert rig.calls["connect"] == 2 and rig.cams[0].defaults_applied


async def test_connect_cooldown_can_be_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(session_mod, "COOL_SLICE_S", 0.1)
    rig = Rig(tmp_path, cooldown_s=60.0)
    rig.session.connect_wait_s = 0.1
    async with Client(rig.server) as c:
        r = await call(c, "camera_connect")
        job_id = r.structured_content["job"]["job_id"]
        await sleep(0.4)
        await call(c, "job_cancel", job_id=job_id)
        done = await job_wait(c, job_id)
        assert done["state"] == "cancelled"
        assert not rig.session.connected and rig.calls["disconnect"] >= 1


async def test_connect_access_denied_fails_fast_with_hint(tmp_path):
    rig = Rig(tmp_path, connect_fault="access_denied")
    async with Client(rig.server) as c:
        t0 = time.monotonic()
        r = await call(c, "camera_connect")
        assert r.is_error
        assert "ACCESS_DENIED" in text(r) and "Another client" in text(r)
        assert time.monotonic() - t0 < 3
        assert not rig.session.connected
        r = await call(c, "camera_status")  # the failed job must not leave the session busy
        assert r.structured_content["live"] is True


async def test_connect_timeout_disconnects(tmp_path):
    rig = Rig(tmp_path, connect_fault="timeout")
    async with Client(rig.server) as c:
        r = await call(c, "camera_connect")
        assert r.is_error and "did not answer in time" in text(r)
        assert not rig.session.connected


async def test_tools_need_a_connection(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        for name, args in [("camera_grab", {}), ("camera_configure", {"width": 64}),
                           ("buffer_record", {}), ("buffer_download", {})]:  # fmt: skip
            r = await call(c, name, **args)
            assert r.is_error and "camera_connect" in text(r), name


# ----- configure ----------------------------------------------------------------------------
async def test_configure_order_and_actual_values(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        r = await call(
            c, "camera_configure", calibration_mode="NUC", frame_rate=200.0,
            integration_time_us=250.0, offset_x=64, width=128, height=64,
        )  # fmt: skip
        assert not r.is_error, text(r)
        assert rig.world.log == [
            "set_resolution", "set_roi_offset", "set_integration_time",
            "set_frame_rate", "set_calibration_mode",
        ]  # fmt: skip
        sc = r.structured_content
        assert sc["applied"]["width"] == 128 and sc["applied"]["height"] == 64
        assert sc["applied"]["offset_x"] == 64 and sc["applied"]["calibration_mode"] == "NUC"
        assert sc["clamped"] == []
        r = await call(c, "camera_status")
        assert r.structured_content["settings"]["calibration"] == "NUC"


async def test_configure_reports_clamps(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        r = await call(c, "camera_configure", frame_rate=5000.0, integration_time_us=99999.0)
        sc = r.structured_content
        fields = {x["field"]: x for x in sc["clamped"]}
        assert fields["frame_rate"]["actual"] == pytest.approx(3100.0)
        assert fields["integration_time_us"]["actual"] == 10000.0
        assert sc["warnings"]


async def test_configure_errors(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        r = await call(c, "camera_configure")
        assert r.is_error and "at least one" in text(r)
        r = await call(c, "camera_configure", integration_time_us=100.0, width=100)
        assert r.is_error and "Invalid argument" in text(r)
        assert "Already applied" not in text(r)  # the resolution is the first write
        r = await call(c, "camera_configure", calibration_mode="BOGUS")
        assert r.is_error  # rejected by the schema


# ----- grab ---------------------------------------------------------------------------------
async def test_grab_returns_text_structured_and_image(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        r = await call(c, "camera_grab", name="first")
        assert not r.is_error, text(r)
        kinds = [x.type for x in r.content]
        assert kinds == ["text", "image"]
        assert r.content[1].mime_type == "image/png"
        sc = r.structured_content
        assert sc["shape"] == [1, 256, 320] and sc["units"] == "degC"
        files = rig.data / "grabs"
        assert np.load(files / "first.npy").shape == (1, 256, 320)
        assert (files / "first.png").read_bytes().startswith(b"\x89PNG")
        meta = json.loads((files / "first.json").read_text())
        assert meta["calibration_mode"] == "RT" and meta["stats"]["max"] >= meta["stats"]["min"]
        # a name is never overwritten
        r = await call(c, "camera_grab", name="first")
        assert r.is_error and "already exists" in text(r)
        r = await call(c, "camera_grab", name="../evil")
        assert r.is_error and "Invalid name" in text(r)
        r = await call(c, "camera_grab", n_frames=3, convert=False)
        assert r.structured_content["dtype"] == "uint16"
        assert r.structured_content["shape"] == [3, 256, 320]


# ----- record -------------------------------------------------------------------------------
async def test_record_job_completes(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        r = await call(c, "buffer_record", frames_per_seq=50, n_sequences=2, pre_moi=10)
        assert not r.is_error, text(r)
        done = await job_wait(c, r.structured_content["job_id"])
        assert done["state"] == "done", done
        seqs = done["result"]["sequences"]
        assert [s["frames"] for s in seqs] == [50, 50]
        assert seqs[0]["moi_index"] == 10
        assert rig.calls["buffer_arm"] == 1 and rig.calls["buffer_fire_moi"] == 2
        assert rig.calls["acquisition_stop"] == 1  # never left armed
        r = await call(c, "buffer_record", frames_per_seq=5, duration_s=1.0)
        assert r.is_error and "not both" in text(r)


async def test_record_job_can_be_cancelled(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        await call(c, "camera_configure", frame_rate=10.0)
        r = await call(c, "buffer_record", frames_per_seq=100)  # 10 s
        job_id = r.structured_content["job_id"]
        await sleep(0.3)
        t0 = time.monotonic()
        r = await call(c, "job_cancel", job_id=job_id)
        assert r.structured_content["state"] == "cancelled"
        assert time.monotonic() - t0 < 3
        assert rig.calls["acquisition_stop"] >= 1 and rig.cams[0]._armed_at is None
        assert rig.world.seq_frames == {}


# ----- download -----------------------------------------------------------------------------
def small_frames(rig, n):
    rig.world.width, rig.world.height = 64, 4
    rig.world.seq_frames[0] = n


async def test_download_job_writes_memmap(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        small_frames(rig, 2500)
        r = await call(c, "buffer_download", name="rec1")
        assert not r.is_error, text(r)
        done = await job_wait(c, r.structured_content["job_id"])
        assert done["state"] == "done", done
        arr = np.load(rig.data / "downloads" / "rec1.npy", mmap_mode="r")
        assert arr.shape == (2500, 6, 64) and arr.dtype == np.uint16
        from pyTelops import header_frame_ids

        ids = header_frame_ids(np.asarray(arr))
        assert ids[0] == 1000 and ids[-1] == 3499 and np.all(np.diff(ids) == 1)
        # slices use frame IDs (start_frame), never 0 for n_frames
        assert rig.cams[0].download_calls == [(1000, 1000), (2000, 1000), (3000, 500)]
        meta = json.loads((rig.data / "downloads" / "rec1.json").read_text())
        assert meta["complete"] and meta["state"] == "done" and meta["frames_written"] == 2500
        assert meta["shape"] == [2500, 6, 64] and meta["calibration_mode"] == "RT"
        assert meta["header"]["data_exp"] == -8 and meta["header"]["calibration_mode"] == 2
        assert meta["download_stats"]["slices"] == 3 and meta["integrity_ok"] is True
        # the conversion in the docs reproduces the Celsius frames
        raw = np.asarray(arr[0, 2:, :], dtype=np.float32)
        h = meta["header"]
        celsius = raw * 2.0 ** h["data_exp"] + h["data_offset"] - 273.15
        assert celsius.shape == (4, 64) and np.isfinite(celsius).all()


async def test_download_job_can_be_cancelled_midway(tmp_path):
    rig = Rig(tmp_path, slice_delay_s=0.4)
    async with Client(rig.server) as c:
        await connect(c)
        small_frames(rig, 2500)
        r = await call(c, "buffer_download", name="partial")
        job_id = r.structured_content["job_id"]
        await sleep(0.6)  # first slice done, second running
        await call(c, "job_cancel", job_id=job_id)
        done = await job_wait(c, job_id)
        assert done["state"] == "cancelled"
        meta = json.loads((rig.data / "downloads" / "partial.json").read_text())
        assert meta["complete"] is False and meta["state"] == "cancelled"
        assert 0 < meta["frames_written"] < 2500
        assert (rig.data / "downloads" / "partial.npy").exists()
        # the camera is usable again
        r = await call(c, "camera_status")
        assert r.structured_content["live"] is True


async def test_download_integrity_error_is_reported(tmp_path):
    rig = Rig(tmp_path, download_fault="integrity")
    async with Client(rig.server) as c:
        await connect(c)
        small_frames(rig, 100)
        r = await call(c, "buffer_download", name="bad")
        done = await job_wait(c, r.structured_content["job_id"])
        assert done["state"] == "failed"
        assert "Frame integrity check failed" in done["error"] and "Retry" in done["error"]
        meta = json.loads((rig.data / "downloads" / "bad.json").read_text())
        assert meta["complete"] is False and meta["state"] == "failed"


async def test_download_empty_sequence(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        r = await call(c, "buffer_download")
        done = await job_wait(c, r.structured_content["job_id"])
        assert done["state"] == "failed" and "no frames" in done["error"]


# ----- busy handling and cached status ---------------------------------------------------------
async def test_status_during_job_uses_cache(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        await call(c, "camera_configure", frame_rate=10.0)
        before = (rig.calls["buffer_info"], rig.calls["buffer_status"])
        r = await call(c, "buffer_record", frames_per_seq=100)
        job_id = r.structured_content["job_id"]
        for _ in range(3):
            await sleep(0.1)
            s = (await call(c, "camera_status")).structured_content
            assert s["live"] is False and s["job"]["state"] == "running"
            assert s["settings"]["width"] == 320 and s["snapshot_age_s"] is not None
        # the record job itself never calls them either; no register writes behind our back
        assert (rig.calls["buffer_info"], rig.calls["buffer_status"]) == before
        # other tools refuse at once instead of queueing
        for name, args in [("camera_grab", {}), ("camera_configure", {"width": 64}),
                           ("buffer_record", {}), ("buffer_clear", {"confirm": True}),
                           ("camera_disconnect", {})]:  # fmt: skip
            t0 = time.monotonic()
            r = await call(c, name, **args)
            assert r.is_error and job_id in text(r), name
            assert time.monotonic() - t0 < 1
        await call(c, "job_cancel", job_id=job_id)
        s = (await call(c, "camera_status")).structured_content
        assert s["live"] is True
        assert rig.calls["buffer_info"] > before[0]  # idle status reads it again


# ----- destructive tools -------------------------------------------------------------------------
async def test_clear_and_nuc_need_confirm(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        r = await call(c, "buffer_clear")
        assert r.is_error and "confirm=true" in text(r)
        r = await call(c, "buffer_clear", confirm=False)
        assert r.is_error and rig.calls["buffer_clear"] == 0
        r = await call(c, "camera_nuc")
        assert r.is_error and "lens" in text(r) and rig.calls["nuc"] == 0
        r = await call(c, "camera_nuc", mode="icu")
        assert r.is_error and "lens" not in text(r)
        assert (await call(c, "buffer_clear", confirm=True)).is_error is False
        assert (await call(c, "camera_nuc", confirm=True)).is_error is False
        assert rig.calls["buffer_clear"] == 1 and rig.calls["nuc"] == 1


# ----- errors ------------------------------------------------------------------------------------
async def test_unexpected_driver_exception_is_wrapped(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)

        def boom(*a, **k):
            raise KeyError("odd")

        rig.cams[0].grab = boom
        r = await call(c, "camera_grab")
        assert r.is_error and "KeyError" in text(r)


async def test_job_and_doctor_tools(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        r = await call(c, "job_status", job_id="nope")
        assert r.is_error and "Unknown job" in text(r)
        r = await call(c, "camera_doctor")
        assert not r.is_error and len(r.structured_content["problems"]) == 1
        await connect(c)
        r = await call(c, "camera_doctor", stream=True)
        assert r.is_error and "camera_disconnect" in text(r)
        assert not (await call(c, "camera_doctor")).is_error


# ----- shutdown and stdout -------------------------------------------------------------------------
async def test_shutdown_cancels_job_and_disconnects_once(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        await call(c, "camera_configure", frame_rate=10.0)
        r = await call(c, "buffer_record", frames_per_seq=100)
        job_id = r.structured_content["job_id"]
        await sleep(0.2)
    job = rig.session.jobs[job_id]
    assert job.state == "cancelled"
    assert rig.calls["disconnect"] == 1
    assert rig.session.camera is None
    await rig.session.shutdown()
    assert rig.calls["disconnect"] == 1


async def test_shutdown_without_camera(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server):
        pass
    assert rig.calls["disconnect"] == 0


async def test_nothing_is_written_to_stdout(tmp_path, capsys):
    original = sys.stdout
    rig = Rig(tmp_path, cooldown_s=0.3)
    async with Client(rig.server) as c:
        await connect(c)  # the simulated connect prints, like the real one
        await call(c, "camera_grab")
        await call(c, "buffer_record", frames_per_seq=20)
        r = await call(c, "camera_status")
        await job_wait(c, r.structured_content["job"]["job_id"])
        small_frames(rig, 30)
        r = await call(c, "buffer_download")
        await job_wait(c, r.structured_content["job_id"])
        await call(c, "camera_doctor")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Discovered a simulated camera" in captured.err  # it went to stderr
    assert sys.stdout is original


# ----- review fixes ------------------------------------------------------------------------------------
async def test_buffer_mode_is_turned_off_before_grab(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        r = await call(c, "buffer_record", frames_per_seq=20)
        assert (await job_wait(c, r.structured_content["job_id"]))["state"] == "done"
        assert rig.world.buffer_mode is True  # record leaves the mode on
        # recordings are held: turning the mode off could lose them, so the tool refuses
        r = await call(c, "camera_grab")
        assert r.is_error and "buffer_download" in text(r) and "buffer_clear" in text(r)
        assert rig.world.buffer_mode is True
        assert not (await call(c, "buffer_clear", confirm=True)).is_error
        assert rig.world.buffer_mode is True  # clear re-applies the configuration
        r = await call(c, "camera_grab")
        assert not r.is_error, text(r)
        assert rig.world.buffer_mode is False
        rig.world.buffer_mode = True  # configure needs live mode too
        assert not (await call(c, "camera_configure", integration_time_us=100.0)).is_error
        assert rig.world.buffer_mode is False


async def test_sim_refuses_grab_in_buffer_mode(tmp_path):
    cam = SimCamera(world=SimWorld())
    cam.connect()
    cam.buffer_configure(frames_per_seq=10)
    with pytest.raises(GVCPError):
        cam.grab()
    with pytest.raises(GVCPError):
        cam.acquire(2)


async def test_record_refuses_when_the_camera_holds_recordings(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        r = await call(c, "buffer_record", frames_per_seq=20)
        assert (await job_wait(c, r.structured_content["job_id"]))["state"] == "done"
        configured = rig.calls["buffer_configure"]
        r = await call(c, "buffer_record", frames_per_seq=20)
        done = await job_wait(c, r.structured_content["job_id"])
        assert done["state"] == "failed" and "buffer_clear" in done["error"]
        assert rig.calls["buffer_configure"] == configured  # nothing was touched
        assert rig.world.seq_frames == {0: 20}
        await call(c, "buffer_clear", confirm=True)
        r = await call(c, "buffer_record", frames_per_seq=20)
        assert (await job_wait(c, r.structured_content["job_id"]))["state"] == "done"


async def test_record_fails_when_the_frame_count_is_wrong(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        cam = rig.cams[0]
        real = cam.buffer_recorded_frames
        cam.buffer_recorded_frames = lambda sequence=0: real(sequence) - 1
        r = await call(c, "buffer_record", frames_per_seq=20)
        done = await job_wait(c, r.structured_content["job_id"])
        assert done["state"] == "failed" and "20 configured" in done["error"]


def test_sim_configure_keeps_recordings():
    cam = SimCamera(world=SimWorld())
    cam.connect()
    cam.world.seq_frames[0] = 7
    cam.buffer_configure(frames_per_seq=10)
    assert cam.world.seq_frames == {0: 7}


async def test_integrity_flags_wrong_frame_ids(tmp_path):
    rig = Rig(tmp_path, download_fault="wrong_ids")
    async with Client(rig.server) as c:
        await connect(c)
        small_frames(rig, 50)
        r = await call(c, "buffer_download", name="ids")
        done = await job_wait(c, r.structured_content["job_id"])
        assert done["state"] == "done" and done["result"]["integrity_ok"] is False
        meta = json.loads((rig.data / "downloads" / "ids.json").read_text())
        assert meta["n_frame_id_mismatch"] == 50 and meta["integrity_ok"] is False
        assert meta["complete"] is True


async def test_integrity_flags_order_problems_from_the_driver(tmp_path):
    rig = Rig(tmp_path, download_fault="out_of_order")
    async with Client(rig.server) as c:
        await connect(c)
        small_frames(rig, 50)
        r = await call(c, "buffer_download", name="ooo")
        done = await job_wait(c, r.structured_content["job_id"])
        meta = json.loads((rig.data / "downloads" / "ooo.json").read_text())
        assert meta["download_stats"]["n_out_of_order"] == 2
        assert meta["integrity_ok"] is False and done["result"]["integrity_ok"] is False


async def test_download_metadata_comes_from_the_headers(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        small_frames(rig, 30)
        rig.world.cal = CalibrationMode.NUC  # NUC frames carry header value 255
        r = await call(c, "buffer_download", name="hdr")
        await job_wait(c, r.structured_content["job_id"])
        meta = json.loads((rig.data / "downloads" / "hdr.json").read_text())
        assert meta["shape"] == [30, 6, 64] and meta["resolution"] == [64, 4]
        assert meta["calibration_mode"] == "RAW"  # from the header, not cam.calibration_mode
        assert meta["frame_rate_hz"] == 100.0 and meta["header"]["data_exp"] == 0


async def test_job_is_finished_only_after_the_thread_stops_using_the_camera(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        cam = rig.cams[0]
        in_use = threading.Event()
        real = cam.buffer_info

        def slow_info():
            in_use.set()
            time.sleep(0.4)
            try:
                return real()
            finally:
                in_use.clear()

        cam.buffer_info = slow_info
        r = await call(c, "buffer_record", frames_per_seq=10)
        job_id = r.structured_content["job_id"]
        while True:
            s = (await call(c, "job_status", job_id=job_id)).structured_content
            if s["state"] != "running":
                break
            await sleep(0.02)
        assert not in_use.is_set()  # the final snapshot refresh is over
        assert rig.session._active is None


async def test_shutdown_does_not_wait_forever_for_the_lock(tmp_path):
    import anyio

    rig = Rig(tmp_path)
    rig.session.lock_timeout_s = 0.2
    async with Client(rig.server) as c:
        await connect(c)

        async def hold():  # another task: a camera call that never ends
            async with rig.session.lock:
                await anyio.sleep(30)

        async with anyio.create_task_group() as tg:
            tg.start_soon(hold)
            await sleep(0.1)
            t0 = time.monotonic()
            await rig.session.shutdown()
            assert time.monotonic() - t0 < 2
            tg.cancel_scope.cancel()
    assert rig.calls["disconnect"] == 1


async def test_forced_shutdown_is_logged(tmp_path, capsys):
    rig = Rig(tmp_path, grace=0.2, slice_delay_s=1.0)
    async with Client(rig.server) as c:
        await connect(c)
        small_frames(rig, 1500)
        await call(c, "buffer_download", name="stuck")
        await sleep(0.1)
    assert rig.calls["disconnect"] == 1
    assert "still running" in capsys.readouterr().err


async def test_pre_moi_wait_can_be_cancelled(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        await call(c, "camera_configure", frame_rate=10.0)
        r = await call(c, "buffer_record", frames_per_seq=10, n_sequences=2, pre_moi=1000)
        job_id = r.structured_content["job_id"]
        await sleep(1.8)  # first sequence (1 s) done, now waiting 100 s to refill
        t0 = time.monotonic()
        r = await call(c, "job_cancel", job_id=job_id)
        assert r.structured_content["state"] == "cancelled"
        assert time.monotonic() - t0 < 2.5


async def test_connect_resets_download_mode(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
    assert rig.calls["download_mode_write"] == 1


async def test_failed_connect_leaves_nothing_connected(tmp_path, monkeypatch):
    def boom(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(SimCamera, "tdc_status", property(boom))
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        r = await call(c, "camera_connect")
        assert r.is_error and "boom" in text(r)
        assert rig.session.camera is None and not rig.cams[0].is_connected


async def test_configure_moves_the_offset_around_a_resize(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        rig.world.width, rig.world.offset = 128, (192, 0)
        r = await call(c, "camera_configure", width=320)
        assert not r.is_error, text(r)
        assert rig.world.log == ["set_roi_offset", "set_resolution"]
        applied = r.structured_content["applied"]
        assert applied["width"] == 320 and applied["offset_x"] == 0
        assert any("offset_x reset" in w for w in r.structured_content["warnings"])
        # the other way round: shrink first, then move the offset
        rig.world.log.clear()
        r = await call(c, "camera_configure", width=128, offset_x=192)
        assert not r.is_error, text(r)
        assert rig.world.log == ["set_resolution", "set_roi_offset"]
        assert r.structured_content["applied"]["offset_x"] == 192


def test_sim_setters_validate_like_the_driver():
    cam = SimCamera(world=SimWorld())
    cam.connect()
    with pytest.raises(ValueError):
        cam.roi_offset = (32, 0)  # not a multiple of 64
    with pytest.raises(ValueError):
        cam.roi_offset = (0, 2)  # not a multiple of 4
    with pytest.raises(ValueError):
        cam.roi_offset = (64, 0)  # 64 + 320 leaves the sensor
    cam.resolution = (128, 64)
    cam.roi_offset = (192, 192)
    with pytest.raises(GVCPError):
        cam.resolution = (320, 64)  # the camera refuses a window outside the sensor


def test_grab_in_non_rt_mode_returns_counts():
    cam = SimCamera(world=SimWorld())
    cam.connect()
    assert cam.grab(convert=True).dtype == np.float32
    cam.calibration_mode = "NUC"
    assert cam.grab(convert=True).dtype == np.uint16


def test_local_timeout_has_its_own_message():
    msg = explain(GVCPError("read timed out", 0))
    assert "did not answer" in msg and "power-cycle" in msg
    assert "ACCESS_DENIED" not in msg


async def test_record_cleanup_steps_do_not_skip_each_other(tmp_path):
    rig = Rig(tmp_path)
    async with Client(rig.server) as c:
        await connect(c)
        cam = rig.cams[0]
        cleared = []
        real_clear = cam._clear_buffer_arm_state

        def bad_write(addr, value):
            raise GVCPError("write failed", 0)

        cam.write_register = bad_write
        cam._clear_buffer_arm_state = lambda: (cleared.append(1), real_clear())
        await call(c, "camera_configure", frame_rate=10.0)
        r = await call(c, "buffer_record", frames_per_seq=100)
        job_id = r.structured_content["job_id"]
        await sleep(0.3)
        await call(c, "job_cancel", job_id=job_id)
        assert (await job_wait(c, job_id))["state"] == "cancelled"
        assert cleared  # the second cleanup step ran although the first one raised


# ----- the simulator must not drift from the driver ----------------------------------------------------
SIM_METHODS = [
    "connect", "wait_until_ready", "disconnect", "grab", "acquire", "nuc", "sensor_temperature",
    "buffer_configure", "buffer_arm", "buffer_ready_in", "buffer_fire_moi", "buffer_status",
    "buffer_info", "buffer_recorded_frames", "buffer_moi_index", "buffer_download",
    "buffer_clear", "read_register", "write_register", "_buffer_wait_sequence",
    "_clear_buffer_arm_state",
]  # fmt: skip
SIM_PROPERTIES = [
    ("resolution", True), ("roi_offset", True), ("integration_time", True), ("frame_rate", True),
    ("calibration_mode", True), ("frame_rate_max", False), ("info", False), ("tdc_status", False),
    ("is_connected", False),
]  # fmt: skip


@pytest.mark.parametrize("name", SIM_METHODS)
def test_sim_method_signature_matches_driver(name):
    real = inspect.signature(getattr(Camera, name))
    sim = inspect.signature(getattr(SimCamera, name))
    assert list(sim.parameters) == list(real.parameters), name
    for p, rp in real.parameters.items():
        assert sim.parameters[p].default == rp.default or p == "self", (name, p)


@pytest.mark.parametrize(("name", "settable"), SIM_PROPERTIES)
def test_sim_property_matches_driver(name, settable):
    assert isinstance(getattr(Camera, name), property)
    assert isinstance(getattr(SimCamera, name), property)
    assert (getattr(Camera, name).fset is not None) == settable
    assert (getattr(SimCamera, name).fset is not None) == settable
