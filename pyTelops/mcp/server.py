"""MCP server for the Telops camera: tools, output models and the stdio runner."""

from __future__ import annotations

import base64
import contextlib
import os
import sys
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from .. import registers as reg
from ..camera import Camera
from . import artifacts
from .session import Session, explain

WIDTH_MAX, HEIGHT_MAX = Camera.WIDTH_MAX, Camera.HEIGHT_MAX


# ----- output models (the structured part of each result) ------------------------------
class StatusOut(BaseModel):
    connected: bool
    simulated: bool
    live: bool  # False: a job or call is running, the values are the last snapshot
    snapshot_age_s: float | None = None
    settings: dict[str, Any] | None = None
    tdc_status: int | None = None
    buffer: dict[str, Any] | None = None
    job: dict[str, Any] | None = None


class JobOut(BaseModel):
    job_id: str
    kind: str
    state: str  # running, done, failed, cancelled
    progress: float
    message: str
    result: dict[str, Any] | None = None
    error: str | None = None
    elapsed_s: float


class ConnectOut(BaseModel):
    connected: bool
    job: JobOut | None = None
    settings: dict[str, Any] | None = None


class ConfigureOut(BaseModel):
    applied: dict[str, Any]
    clamped: list[dict[str, Any]]
    warnings: list[str]


class GrabOut(BaseModel):
    npy: str
    png: str
    json_path: str
    n_frames: int
    shape: list[int]
    dtype: str
    units: str
    calibration_mode: str
    stats: dict[str, float]


class DoctorOut(BaseModel):
    summary: dict[str, Any]
    problems: list[dict[str, Any]]
    camera: dict[str, Any]


class SimpleOut(BaseModel):
    ok: bool
    message: str


def _result(model: BaseModel, text: str, *images: ImageContent) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text), *images],
        structured_content=model.model_dump(mode="json"),
    )


def _job_out(job_dict: dict) -> JobOut:
    return JobOut(**job_dict)


def _job_text(j: dict) -> str:
    text = f"Job {j['job_id']} ({j['kind']}): {j['state']}, {j['progress']:.0%}"
    if j["message"]:
        text += f". {j['message']}"
    if j["error"]:
        text += f" Error: {j['error']}"
    return text


def _ann(*, read_only=False, destructive=False, idempotent=None) -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=None if read_only else destructive,
        idempotent_hint=idempotent,
        open_world_hint=False,
    )


INSTRUCTIONS = (
    "Controls one Telops thermal camera. Call camera_connect first; a cold camera needs minutes to "
    "cool, so poll job_status. Only one job (connect, record, download) runs at a time and other "
    "tools refuse until it ends. Only one client can control the camera."
)


def build_server(
    data_dir: Path | str,
    camera_factory: Callable[[str | None], Any],
    *,
    simulated: bool = False,
    doctor_fn: Callable | None = None,
    shutdown_grace_s: float = 10.0,
) -> tuple[MCPServer, Session]:
    """Create the server and its session. Tests pass a simulated camera factory."""
    session = Session(
        Path(data_dir),
        camera_factory,
        simulated=simulated,
        doctor_fn=doctor_fn,
        shutdown_grace_s=shutdown_grace_s,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app):
        try:
            yield session
        finally:
            with anyio.CancelScope(shield=True):
                await session.shutdown()

    server = MCPServer("pytelops", instructions=INSTRUCTIONS, lifespan=lifespan)

    # ----- diagnostics and connection ------------------------------------------------
    @server.tool(annotations=_ann(destructive=False))
    async def camera_doctor(stream: bool = False) -> Annotated[CallToolResult, DoctorOut]:
        """Check host, network and camera state and list problems with fixes.

        stream=true also connects and tests frame delivery. That changes camera state
        and needs the camera disconnected.
        """
        report = await session.doctor(stream)
        n = len(report["problems"])
        return _result(
            DoctorOut(**report), f"Doctor found {n} problem(s). Summary: {report['summary']}"
        )

    @server.tool(annotations=_ann(idempotent=True))
    async def camera_connect(ip: str | None = None) -> Annotated[CallToolResult, ConnectOut]:
        """Find and connect to the camera. A cooling camera can take up to 10 minutes.

        Runs as a job. Returns the result at once when the camera is ready, otherwise a
        job to poll with job_status. ip is optional; the default is network discovery.
        """
        if session.connected:
            st = await session.status()
            return _result(
                ConnectOut(connected=True, settings=st["settings"]), "Already connected."
            )
        job = await session.start_job("connect", session.connect_job(ip), needs_camera=False)
        await session.wait_job(job, session.connect_wait_s)
        j = job.as_dict()
        if job.state == "failed":
            raise ToolError(job.error or "Connect failed.")
        if job.state == "done":
            return _result(
                ConnectOut(
                    connected=True, job=_job_out(j), settings=(job.result or {}).get("settings")
                ),
                "Connected.",
            )
        return _result(
            ConnectOut(connected=False, job=_job_out(j)),
            f"Connecting. {_job_text(j)} Poll job_status.",
        )

    @server.tool(annotations=_ann(idempotent=True))
    async def camera_disconnect() -> Annotated[CallToolResult, SimpleOut]:
        """Disconnect from the camera and release control."""
        was = await session.disconnect()
        msg = "Disconnected." if was else "Was not connected."
        return _result(SimpleOut(ok=True, message=msg), msg)

    @server.tool(annotations=_ann(read_only=True))
    async def camera_status() -> Annotated[CallToolResult, StatusOut]:
        """Return connection, settings, buffer state and the current job.

        While a job runs the values come from the last snapshot (live=false).
        """
        st = await session.status()
        if not st["connected"]:
            text = "Not connected."
        else:
            s = st["settings"] or {}
            text = (
                f"Connected. {s.get('width')}x{s.get('height')}, {s.get('frame_rate_hz')} Hz, "
                f"{s.get('calibration')}, sensor {s.get('temperature_c')} C"
                + ("" if st["live"] else " (snapshot, job running)")
                + "."
            )
        return _result(StatusOut(**st), text)

    @server.tool(annotations=_ann(idempotent=True))
    async def camera_configure(
        width: Annotated[int | None, Field(ge=64, le=320)] = None,
        height: Annotated[int | None, Field(ge=4, le=256)] = None,
        offset_x: Annotated[int | None, Field(ge=0)] = None,
        offset_y: Annotated[int | None, Field(ge=0)] = None,
        integration_time_us: Annotated[float | None, Field(gt=0)] = None,
        frame_rate: Annotated[float | None, Field(gt=0)] = None,
        calibration_mode: Literal["RT", "NUC", "RAW", "RAW0", "IBR", "IBI"] | None = None,
    ) -> Annotated[CallToolResult, ConfigureOut]:
        """Set only the given fields, in a safe order, and return the values the camera holds.

        Width is a multiple of 64 and height a multiple of 4. The camera can clamp a
        value; clamped lists every field whose actual value differs from the request.
        """
        asked = {
            "width": width,
            "height": height,
            "offset_x": offset_x,
            "offset_y": offset_y,
            "integration_time_us": integration_time_us,
            "frame_rate": frame_rate,
            "calibration_mode": calibration_mode,
        }
        asked = {k: v for k, v in asked.items() if v is not None}
        if not asked:
            raise ToolError("Give at least one setting to change.")

        def run(cam):
            session.ensure_live_mode(cam)
            done: list[str] = []
            caught: list[str] = []
            centered = False
            with contextlib.suppress(Exception):
                centered = bool(cam.read_register(reg.REG_CENTER_IMAGE))
            try:
                with warnings.catch_warnings(record=True) as wl:
                    warnings.simplefilter("always")
                    if centered and (width is not None or height is not None):
                        # CenterImage on: the camera centers the window itself and refuses
                        # offset writes (on the LADISK camera it is locked on).
                        cw, ch = cam.resolution
                        cam.resolution = (width or cw, height or ch)
                        done.append("resolution")
                    elif width is not None or height is not None:
                        cw, ch = cam.resolution
                        nw, nh = width or cw, height or ch
                        cx, cy = cam.roi_offset
                        # The offset setter checks offset + size against the sensor using the
                        # size the camera has now. A bigger size at the old offset does not
                        # fit, so move the offset out of the way first, then resize, then
                        # write the final offset.
                        fx = (
                            offset_x
                            if offset_x is not None
                            else (cx if cx + nw <= WIDTH_MAX else 0)
                        )
                        fy = (
                            offset_y
                            if offset_y is not None
                            else (cy if cy + nh <= HEIGHT_MAX else 0)
                        )
                        if offset_x is None and fx != cx:
                            caught.append(f"offset_x reset from {cx} to {fx} to fit width {nw}")
                        if offset_y is None and fy != cy:
                            caught.append(f"offset_y reset from {cy} to {fy} to fit height {nh}")
                        px = cx
                        py = cy
                        if cx + nw > WIDTH_MAX:
                            px = fx if fx + cw <= WIDTH_MAX else 0
                        if cy + nh > HEIGHT_MAX:
                            py = fy if fy + ch <= HEIGHT_MAX else 0
                        if (px, py) != (cx, cy):
                            cam.roi_offset = (px, py)
                            done.append("offset")
                        cam.resolution = (nw, nh)
                        done.append("resolution")
                        if (fx, fy) != (px, py):
                            cam.roi_offset = (fx, fy)
                            done.append("offset")
                    elif (offset_x is not None or offset_y is not None) and not centered:
                        cx, cy = cam.roi_offset
                        cam.roi_offset = (
                            cx if offset_x is None else offset_x,
                            cy if offset_y is None else offset_y,
                        )
                        done.append("offset")
                    if integration_time_us is not None:
                        cam.integration_time = integration_time_us
                        done.append("integration_time_us")
                    if frame_rate is not None:
                        cam.frame_rate = frame_rate
                        done.append("frame_rate")
                    if calibration_mode is not None:
                        cam.calibration_mode = calibration_mode
                        done.append("calibration_mode")
                caught += [str(w.message) for w in wl]
            except Exception as exc:
                applied = f" Already applied: {', '.join(done)}." if done else ""
                raise ToolError(explain(exc) + applied) from exc
            w_, h_ = cam.resolution
            ox, oy = cam.roi_offset
            if centered and (
                (offset_x is not None and offset_x != ox)
                or (offset_y is not None and offset_y != oy)
            ):
                applied = f" Already applied: {', '.join(done)}." if done else ""
                raise ToolError(
                    f"The camera centers the window (CenterImage is on), so the offset is "
                    f"({ox}, {oy}) and cannot be set. Give width and height only.{applied}"
                )
            actual = {
                "width": w_,
                "height": h_,
                "offset_x": ox,
                "offset_y": oy,
                "integration_time_us": float(cam.integration_time),
                "frame_rate": float(cam.frame_rate),
                "frame_rate_max": float(cam.frame_rate_max),
                "calibration_mode": cam.calibration_mode.name,
                "center_image": centered,
            }
            clamped = []
            for k, v in asked.items():
                a = actual[k]
                same = (abs(a - v) <= max(1e-3 * abs(v), 0.01)) if isinstance(v, float) else a == v
                if not same:
                    clamped.append({"field": k, "requested": v, "actual": a})
            with contextlib.suppress(Exception):
                session.refresh_snapshot()
            return actual, clamped, caught

        actual, clamped, caught = await session.call(run)
        text = "Configured. " + ", ".join(
            f"{k}={v}" for k, v in actual.items() if k != "frame_rate_max"
        )
        if clamped:
            text += ". Clamped: " + ", ".join(
                f"{c['field']} {c['requested']} -> {c['actual']}" for c in clamped
            )
        return _result(ConfigureOut(applied=actual, clamped=clamped, warnings=caught), text)

    # ----- frames -----------------------------------------------------------------------
    @server.tool(annotations=_ann())
    async def camera_grab(
        n_frames: Annotated[int, Field(ge=1, le=500)] = 1,
        convert: bool = True,
        name: str | None = None,
    ) -> Annotated[CallToolResult, GrabOut]:
        """Grab frames, save .npy, a PNG preview and JSON metadata, and show the preview.

        convert=true gives degrees Celsius in RT mode; false gives raw counts without
        header rows. Files go to <data dir>/grabs.
        """
        if name is not None:
            try:
                artifacts.safe_name(name)
            except ValueError as exc:
                raise ToolError(explain(exc)) from exc
        out_dir = session.data_dir / "grabs"

        def run(cam):
            import numpy as np

            session.ensure_live_mode(cam)
            if n_frames == 1:
                f = cam.grab(timeout=5.0, convert=convert)
                frames = None if f is None else f[None]
            else:
                frames = cam.acquire(n_frames, timeout=30.0, convert=convert)
            if frames is None or len(frames) == 0:
                raise TimeoutError("No frame arrived.")
            frames = np.asarray(frames)
            stem = artifacts.new_stem(out_dir, "grab", name, (".npy", ".png", ".json"))
            cal = cam.calibration_mode.name
            units = (
                "degC"
                if frames.dtype.kind == "f" and cal == "RT"
                else "calibrated"
                if frames.dtype.kind == "f"
                else "counts"
            )
            np.save(out_dir / f"{stem}.npy", frames)
            png = artifacts.preview_png(frames[0])
            (out_dir / f"{stem}.png").write_bytes(png)
            meta = {
                "created_utc": artifacts.utc_iso(),
                "shape": list(frames.shape),
                "dtype": str(frames.dtype),
                "units": units,
                "n_requested": n_frames,
                "calibration_mode": cal,
                "integration_time_us": float(cam.integration_time),
                "frame_rate_hz": float(cam.frame_rate),
                "resolution": list(cam.resolution),
                "sensor_temperature_c": float(cam.sensor_temperature("sensor")),
                "stats": artifacts.frame_stats(frames),
            }
            artifacts.write_json(out_dir / f"{stem}.json", meta)
            with contextlib.suppress(Exception):
                session.refresh_snapshot()
            return stem, frames, units, cal, meta, png

        stem, frames, units, cal, meta, png = await session.call(run)
        out = GrabOut(
            npy=str(out_dir / f"{stem}.npy"),
            png=str(out_dir / f"{stem}.png"),
            json_path=str(out_dir / f"{stem}.json"),
            n_frames=int(frames.shape[0]),
            shape=list(frames.shape),
            dtype=str(frames.dtype),
            units=units,
            calibration_mode=cal,
            stats=meta["stats"],
        )
        s = meta["stats"]
        text = (
            f"Grabbed {out.n_frames} frame(s) {tuple(frames.shape[-2:])} in {units}: "
            f"min {s['min']:.2f}, max {s['max']:.2f}, mean {s['mean']:.2f}. Saved {out.npy}."
        )
        img = ImageContent(type="image", data=base64.b64encode(png).decode(), mime_type="image/png")
        return _result(out, text, img)

    # ----- memory buffer -----------------------------------------------------------------
    @server.tool(annotations=_ann())
    async def buffer_record(
        duration_s: Annotated[float | None, Field(gt=0)] = None,
        frames_per_seq: Annotated[int | None, Field(ge=1)] = None,
        n_sequences: Annotated[int, Field(ge=1, le=64)] = 1,
        pre_moi: Annotated[int, Field(ge=0)] = 0,
    ) -> Annotated[CallToolResult, JobOut]:
        """Record into the camera memory buffer. Runs as a job; poll job_status.

        Give duration_s or frames_per_seq (default 100 frames). A software trigger fires
        as soon as the ring buffer holds pre_moi frames. Refuses while the camera memory
        holds recordings: download them, then call buffer_clear with confirm=true.
        """
        if duration_s is not None and frames_per_seq is not None:
            raise ToolError("Give duration_s or frames_per_seq, not both.")
        # Refuse at once, not later as a failed job. The job checks again before it
        # configures the buffer.
        held = await session.call(session.held_sequences)
        if held:
            raise ToolError(session.held_message(held))
        job = await session.start_job(
            "record", session.record_job(n_sequences, duration_s, frames_per_seq, pre_moi)
        )
        j = job.as_dict()
        return _result(_job_out(j), f"Recording started. {_job_text(j)} Poll job_status.")

    @server.tool(annotations=_ann())
    async def buffer_download(
        sequence: Annotated[int, Field(ge=0)] = 0,
        name: str | None = None,
    ) -> Annotated[CallToolResult, JobOut]:
        """Download one recorded sequence to a raw .npy file. Runs as a job; poll job_status.

        Writes uint16 frames with the two header rows, shape (N, H+2, W), plus a JSON file
        with the data needed to convert to Celsius. Files go to <data dir>/downloads.
        """
        if name is not None:
            try:
                artifacts.safe_name(name)
            except ValueError as exc:
                raise ToolError(explain(exc)) from exc
        job = await session.start_job("download", session.download_job(sequence, name))
        j = job.as_dict()
        return _result(_job_out(j), f"Download started. {_job_text(j)} Poll job_status.")

    @server.tool(annotations=_ann(destructive=True))
    async def buffer_clear(confirm: bool = False) -> Annotated[CallToolResult, SimpleOut]:
        """Delete all recorded sequences in the camera memory. Needs confirm=true.

        Download what you need first. This cannot be undone.
        """
        if not confirm:
            raise ToolError(
                "buffer_clear deletes every recording in the camera memory and cannot be undone. "
                "Download what you need, then call again with confirm=true."
            )

        def run(cam):
            cam.buffer_clear()
            with contextlib.suppress(Exception):
                session.refresh_snapshot()

        await session.call(run)
        return _result(SimpleOut(ok=True, message="Buffer cleared."), "Buffer cleared.")

    @server.tool(annotations=_ann())
    async def camera_nuc(
        mode: Literal["black_body", "icu"] = "black_body",
        confirm: bool = False,
        blackbody_temp_c: float | None = None,
    ) -> Annotated[CallToolResult, SimpleOut]:
        """Run a non-uniformity correction. Needs confirm=true. Takes up to 60 s.

        black_body needs a uniform target in front of the lens: ask the user to cover it
        first. Many settings are locked while the NUC runs.
        """
        if not confirm:
            extra = (
                " In black_body mode ask the user to put a uniform black body or lens cap "
                "in front of the lens before you confirm."
                if mode == "black_body"
                else ""
            )
            raise ToolError(
                "camera_nuc changes the camera correction. Call again with confirm=true." + extra
            )

        def run(cam):
            cam.nuc(mode, blackbody_temp=blackbody_temp_c, timeout=60.0)
            with contextlib.suppress(Exception):
                session.refresh_snapshot()

        await session.call(run)
        return _result(SimpleOut(ok=True, message=f"NUC ({mode}) done."), f"NUC ({mode}) done.")

    # ----- jobs -----------------------------------------------------------------------------
    @server.tool(annotations=_ann(read_only=True))
    async def job_status(job_id: str) -> Annotated[CallToolResult, JobOut]:
        """Return state, progress, message and, when finished, the result or error of a job."""
        j = session.get_job(job_id).as_dict()
        return _result(_job_out(j), _job_text(j))

    @server.tool(annotations=_ann())
    async def job_cancel(job_id: str) -> Annotated[CallToolResult, JobOut]:
        """Ask a job to stop. It stops at its next check, within a few seconds."""
        job = session.get_job(job_id)
        if not job.finished:
            job.cancel_flag.set()
            await session.wait_job(job, 3.0)
        j = job.as_dict()
        return _result(_job_out(j), _job_text(j))

    return server, session


def default_factory(simulate: bool, **sim_kwargs) -> Callable[[str | None], Any]:
    """Return the camera factory for the real driver or the simulator."""
    if simulate:
        from .sim import SimCamera, SimWorld

        world = SimWorld(**{k: v for k, v in sim_kwargs.items() if k == "cooldown_s"})
        return lambda ip: SimCamera(ip=ip, world=world)

    return lambda ip: Camera(ip=ip)


def _sim_doctor(ip=None, stream=False, frames=20):
    checks = [{"name": "simulated", "status": "ok", "detail": "simulated camera", "fix": ""}]
    return checks, {"ip": "192.0.2.10", "model": "simulated"}


def serve(data_dir: str | Path = "./pytelops-data", simulate: bool = False) -> None:
    """Run the server on stdio until stdin closes or SIGINT/SIGTERM arrives."""
    server, session = build_server(
        data_dir,
        default_factory(simulate),
        simulated=simulate,
        doctor_fn=_sim_doctor if simulate else None,
    )

    async def main() -> None:
        async with anyio.create_task_group() as tg:

            async def run_server() -> None:
                await server.run_stdio_async()
                tg.cancel_scope.cancel()  # stdin closed

            async def watch_signals() -> None:
                import signal

                with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
                    async for _ in signals:
                        # Stop the job and disconnect first. The SDK's stdin reader sits in a
                        # worker thread that cannot be cancelled, so cancelling the task group
                        # would hang until stdin closes. Exit once the camera is released.
                        with anyio.CancelScope(shield=True):
                            await session.shutdown()
                        sys.stderr.flush()
                        os._exit(0)

            tg.start_soon(run_server)
            if sys.platform != "win32":
                tg.start_soon(watch_signals)

    with contextlib.suppress(KeyboardInterrupt):
        anyio.run(main)
