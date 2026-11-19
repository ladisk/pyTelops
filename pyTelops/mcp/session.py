"""Camera session for the MCP server: one camera, one lock, one background job.

Rules that keep the camera safe:

* Driver calls run one at a time. Short calls hold the ``anyio`` lock. A long
  job (connect, record, download) runs in its own thread and marks the session
  busy; every other camera tool then refuses at once.
* ``buffer_info`` and ``buffer_status`` write registers, so status never calls
  them while a job runs. It returns the snapshot taken after the last call.
* Nothing writes to stdout: the transport owns it. Driver prints go to stderr.
"""

from __future__ import annotations

import contextlib
import shutil
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import anyio
import numpy as np
from mcp.server.mcpserver.exceptions import ToolError
from pyGigEVision import GVCPError

from .. import registers as reg
from ..errors import FrameIntegrityError
from ..header import header_frame_ids, parse_header
from . import artifacts

SLICE_FRAMES = 1000  # frames per buffer_download call (the driver's own chunk size)
COOLING_BUDGET_S = 600.0  # longest wait for a cooling camera
COOL_SLICE_S = 3.0  # length of one cancellable wait while the camera cools
KEEP_JOBS = 5
LOCK_TIMEOUT_S = 5.0  # shutdown waits this long for a running camera call
CONNECT_WAIT_S = 8.0  # how long camera_connect waits before it returns a job
ACCESS_DENIED = 0x8006

_stdout_mtx = threading.Lock()
_stdout_depth = 0
_stdout_saved = None


@contextlib.contextmanager
def quiet_stdout():
    """Send ``sys.stdout`` to stderr while driver code runs (safe across threads)."""
    global _stdout_depth, _stdout_saved
    with _stdout_mtx:
        if _stdout_depth == 0:
            _stdout_saved = sys.stdout
            sys.stdout = sys.stderr
        _stdout_depth += 1
    try:
        yield
    finally:
        with _stdout_mtx:
            _stdout_depth -= 1
            if _stdout_depth == 0:
                sys.stdout = _stdout_saved
                _stdout_saved = None


def explain(exc: BaseException) -> str:
    """Return an error message with a hint on how to fix it."""
    if isinstance(exc, ToolError):
        return str(exc)
    if isinstance(exc, GVCPError):
        status = getattr(exc, "status", 0)
        label = getattr(exc, "status_name", "") or hex(status)
        if status == ACCESS_DENIED:
            return (
                f"Camera refused control (ACCESS_DENIED): {exc}. Another client holds the "
                "camera. Close other Telops software, Python sessions and viewers, then retry. "
                "A lock left by a crashed session expires after about 15 s."
            )
        if status == 0:  # raised locally after all retries, the camera sent no answer
            return (
                f"The camera did not answer: {exc}. Check the cable and the network adapter, "
                "then power-cycle the camera."
            )
        return (
            f"Camera rejected a register access ({label}): {exc}. "
            "The setting may be locked while recording, in NUC, or still cooling. "
            "Check camera_status. If it persists, power-cycle the camera."
        )
    if isinstance(exc, FrameIntegrityError):
        st = exc.stats
        return (
            f"Frame integrity check failed: {exc} ({st.n_incomplete} incomplete frames). "
            "The frames are still in the camera buffer. Retry buffer_download and reduce "
            "other network load."
        )
    if isinstance(exc, TimeoutError):
        return (
            f"The camera did not answer in time: {exc} "
            "Check camera_status, the cable and the cooldown."
        )
    if isinstance(exc, FileExistsError):
        return str(exc)
    if isinstance(exc, OSError):
        return f"Network or disk error: {exc}"
    if isinstance(exc, ValueError):
        return f"Invalid argument: {exc}"
    if isinstance(exc, RuntimeError):
        if "No Telops camera" in str(exc) or "not on any host NIC" in str(exc):
            return f"{exc} Run camera_doctor for a diagnosis."
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _log(msg: str) -> None:
    print(f"pytelops-mcp: {msg}", file=sys.stderr, flush=True)


class JobCancelledError(Exception):
    """Raised inside a job when its cancel flag is set."""


@dataclass
class Job:
    """One background job and its progress."""

    id: str
    kind: str
    state: str = "running"  # running, done, failed, cancelled
    progress: float = 0.0
    message: str = ""
    result: dict | None = None
    error: str | None = None
    started: float = field(default_factory=time.monotonic)
    ended: float | None = None
    cancel_flag: threading.Event = field(default_factory=threading.Event)

    @property
    def finished(self) -> bool:
        return self.state != "running"

    def check_cancel(self) -> None:
        if self.cancel_flag.is_set():
            raise JobCancelledError()

    def update(self, progress: float | None = None, message: str | None = None) -> None:
        if progress is not None:
            self.progress = max(0.0, min(1.0, progress))
        if message is not None:
            self.message = message

    def as_dict(self) -> dict:
        end = self.ended if self.ended is not None else time.monotonic()
        return {
            "job_id": self.id,
            "kind": self.kind,
            "state": self.state,
            "progress": round(self.progress, 3),
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "elapsed_s": round(end - self.started, 1),
        }


class Session:
    """Owns the camera object, the lock, the jobs and the status snapshot."""

    def __init__(
        self,
        data_dir: Path,
        camera_factory: Callable[[str | None], Any],
        *,
        simulated: bool = False,
        doctor_fn: Callable | None = None,
        shutdown_grace_s: float = 10.0,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.camera_factory = camera_factory
        self.simulated = simulated
        self.doctor_fn = doctor_fn
        self.shutdown_grace_s = shutdown_grace_s
        self.connect_wait_s = CONNECT_WAIT_S
        self.lock_timeout_s = LOCK_TIMEOUT_S
        self.camera = None
        self.lock = anyio.Lock()
        self._active: Job | None = None
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self._snapshot: dict | None = None
        self._snapshot_at: float | None = None
        self._closed = False

    # ----- guards -----------------------------------------------------
    @property
    def connected(self) -> bool:
        return self.camera is not None and bool(self.camera.is_connected)

    def _refuse_if_busy(self) -> None:
        if self._active is not None:
            j = self._active
            raise ToolError(
                f"Job {j.id} ({j.kind}) is running. Poll job_status, or stop it with job_cancel."
            )

    def require_camera(self):
        if not self.connected:
            raise ToolError("The camera is not connected. Call camera_connect first.")
        return self.camera

    # ----- synchronous driver calls -------------------------------------
    async def call(self, fn: Callable, *args, needs_camera: bool = True, **kwargs):
        """Run ``fn(camera, ...)`` in a worker thread under the lock."""
        async with self.lock:
            self._refuse_if_busy()
            cam = self.require_camera() if needs_camera else self.camera
            try:
                return await anyio.to_thread.run_sync(lambda: self._run(fn, cam, *args, **kwargs))
            except ToolError:
                raise
            except Exception as exc:
                raise ToolError(explain(exc)) from exc

    def _run(self, fn, cam, *args, **kwargs):
        with quiet_stdout():
            return fn(cam, *args, **kwargs)

    # ----- jobs ----------------------------------------------------------
    async def start_job(
        self, kind: str, fn: Callable[[Job], dict], *, needs_camera: bool = True
    ) -> Job:
        """Start *fn* in a background thread. Only one job runs at a time."""
        async with self.lock:
            self._refuse_if_busy()
            if needs_camera:
                self.require_camera()
            job = Job(id=uuid.uuid4().hex[:8], kind=kind)
            self.jobs[job.id] = job
            while len(self.jobs) > KEEP_JOBS + 1:
                oldest = next(iter(self.jobs))
                if self.jobs[oldest].finished:
                    del self.jobs[oldest]
                else:
                    break
            self._active = job
            threading.Thread(
                target=self._job_main, args=(job, fn), name=f"pytelops-job-{job.id}", daemon=True
            ).start()
        return job

    def _job_main(self, job: Job, fn: Callable[[Job], dict]) -> None:
        result, error, state = None, None, "done"
        try:
            with quiet_stdout():
                result = fn(job)
        except JobCancelledError:
            state = "cancelled"
        except BaseException as exc:  # noqa: BLE001 - the job must always finish
            state, error = "failed", explain(exc)
        # Order matters: the thread stops using the camera first, then the session
        # becomes idle, and only then does the job report its final state.
        with contextlib.suppress(Exception), quiet_stdout():
            if self.connected and job.kind != "connect":
                self.refresh_snapshot()
        self._active = None
        job.ended = time.monotonic()
        job.result, job.error = result, error
        if state == "done":
            job.progress = 1.0
        elif state == "cancelled":
            job.message = "Cancelled."
        job.state = state  # last: wait_job and job_status read this to see "finished"

    def get_job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise ToolError(f"Unknown job {job_id!r}. Known: {', '.join(self.jobs) or 'none'}.")
        return job

    async def wait_job(self, job: Job, seconds: float) -> None:
        """Wait up to *seconds* for the job to finish."""
        deadline = time.monotonic() + seconds
        while not job.finished and time.monotonic() < deadline:
            await anyio.sleep(0.05)

    # ----- status ----------------------------------------------------------
    def refresh_snapshot(self, cam=None) -> None:
        """Read the camera state. Call only when no other driver call is running."""
        cam = cam or self.camera
        snap: dict = {}
        info = dict(cam.info)
        snap["settings"] = {k: (list(v) if isinstance(v, tuple) else v) for k, v in info.items()}
        snap["tdc_status"] = int(cam.tdc_status)
        try:
            snap["buffer"] = cam.buffer_info()
        except Exception as exc:  # noqa: BLE001 - buffer may be unconfigured
            snap["buffer"] = {"error": str(exc)}
        self._snapshot, self._snapshot_at = snap, time.monotonic()

    async def status(self) -> dict:
        """Return the status dict. Live when idle, cached while busy."""
        out: dict = {"connected": self.connected, "simulated": self.simulated}
        busy = self._active is not None or self.lock.locked()
        if self.connected and not busy:
            async with self.lock:
                if self._active is None:
                    try:
                        await anyio.to_thread.run_sync(
                            lambda: self._run(lambda c: self.refresh_snapshot(), self.camera)
                        )
                    except Exception as exc:  # noqa: BLE001
                        raise ToolError(explain(exc)) from exc
                else:
                    busy = True
        snap = self._snapshot
        out["live"] = not busy
        out["snapshot_age_s"] = (
            round(time.monotonic() - self._snapshot_at, 1)
            if snap is not None and self._snapshot_at is not None
            else None
        )
        out["settings"] = snap.get("settings") if snap else None
        out["tdc_status"] = snap.get("tdc_status") if snap else None
        out["buffer"] = snap.get("buffer") if snap else None
        job = self._active or (next(reversed(self.jobs.values())) if self.jobs else None)
        out["job"] = job.as_dict() if job else None
        return out

    async def disconnect(self) -> bool:
        """Disconnect the camera. Returns False when none was connected."""
        async with self.lock:
            self._refuse_if_busy()
            cam, self.camera = self.camera, None
            self._snapshot = self._snapshot_at = None
            if cam is None:
                return False
            try:
                await anyio.to_thread.run_sync(lambda: self._run(lambda c: c.disconnect(), cam))
            except Exception as exc:  # noqa: BLE001
                raise ToolError(explain(exc)) from exc
            return True

    async def doctor(self, stream: bool, frames: int = 20) -> dict:
        """Run ``pyTelops.doctor``. The stream test needs the camera disconnected."""
        if stream and self.connected:
            raise ToolError(
                "The stream test needs control of the camera. Call camera_disconnect first."
            )
        fn = self.doctor_fn
        if fn is None:
            from .. import doctor as _doctor

            fn = _doctor.run_doctor
        async with self.lock:
            self._refuse_if_busy()
            try:
                checks, camera = await anyio.to_thread.run_sync(
                    lambda: self._run(lambda c: fn(ip=None, stream=stream, frames=frames), None)
                )
            except Exception as exc:  # noqa: BLE001
                raise ToolError(explain(exc)) from exc
        from .. import doctor as _doctor

        return _doctor.report_dict(checks, camera)

    # ----- shutdown ----------------------------------------------------------
    async def shutdown(self) -> None:
        """Cancel the running job, then disconnect once."""
        if self._closed:
            return
        self._closed = True
        job = self._active
        if job is not None and not job.finished:
            job.cancel_flag.set()
            await self.wait_job(job, self.shutdown_grace_s)
            if not job.finished:
                _log(
                    f"job {job.id} ({job.kind}) still running after {self.shutdown_grace_s:.0f} s; "
                    "disconnecting anyway"
                )
        cam, self.camera = self.camera, None
        if cam is None:
            return
        with anyio.CancelScope(shield=True):
            got_lock = False
            with anyio.move_on_after(self.lock_timeout_s):
                await self.lock.acquire()
                got_lock = True
            if not got_lock:
                _log(
                    f"a camera call still holds the lock after {self.lock_timeout_s:.0f} s; "
                    "disconnecting anyway"
                )
            try:
                with contextlib.suppress(Exception):
                    await anyio.to_thread.run_sync(lambda: self._run(lambda c: c.disconnect(), cam))
            finally:
                if got_lock:
                    self.lock.release()

    # ----- recordings held in the camera memory ---------------------------------------
    @staticmethod
    def held_sequences(cam) -> int:
        """Number of recorded sequences in the camera memory (0 if the read fails)."""
        with contextlib.suppress(GVCPError):
            return int(cam.read_register(reg.REG_MEMORY_BUFFER_SEQ_COUNT))
        return 0

    @staticmethod
    def held_message(held: int) -> str:
        return (
            f"The camera memory already holds {held} recorded sequence(s). Recording "
            "now could destroy them. Download them with buffer_download, then call "
            "buffer_clear with confirm=true, then record again."
        )

    # ----- helpers for tools that need the live (non-buffer) mode ---------------------
    def ensure_live_mode(self, cam) -> None:
        """Turn the memory buffer mode off before grab or configure, if it is on.

        Buffer mode stays on after buffer_record and buffer_clear (clear re-applies the
        last configuration). Frames grabbed in that mode are not live frames.
        It is not proven that recordings survive mode OFF, so with recordings held
        the call refuses and tells the agent to download and clear first.
        """
        try:
            on = int(cam.read_register(reg.REG_MEMORY_BUFFER_MODE)) != int(reg.MemoryBufferMode.OFF)
        except GVCPError:
            return
        if not on:
            return
        held = 0
        with contextlib.suppress(GVCPError):
            held = int(cam.read_register(reg.REG_MEMORY_BUFFER_SEQ_COUNT))
        if held:
            raise ToolError(
                f"Buffer mode is on and the camera memory holds {held} recorded sequence(s). "
                "Turning it off could lose them. Download them with buffer_download, then call "
                "buffer_clear with confirm=true, then retry."
            )
        with contextlib.suppress(GVCPError):
            cam.write_register(reg.REG_ACQUISITION_STOP, 1)
        cam.write_register(reg.REG_MEMORY_BUFFER_MODE, int(reg.MemoryBufferMode.OFF))
        time.sleep(0.3)

    # ----- job bodies ----------------------------------------------------------
    def connect_job(self, ip: str | None) -> Callable[[Job], dict]:
        def run(job: Job) -> dict:
            cam = self.camera_factory(ip)
            job.update(0.0, "Looking for the camera")
            try:
                try:
                    # Near-zero cooling budget: a cooling camera raises TimeoutError at once,
                    # still connected but without defaults, so the wait below can be cancelled.
                    cam.connect(timeout=10.0, cooling_timeout=0.0, force_control=False)
                except TimeoutError as exc:
                    if "still cooling" not in str(exc):
                        raise
                    self._wait_for_cooldown(cam, job)
                    # Ready now. connect() on a connected camera returns early and skips the
                    # defaults, so release control and connect again.
                    cam.disconnect()
                    job.update(0.95, "Camera ready, applying defaults")
                    cam.connect(timeout=10.0, cooling_timeout=10.0)
                # A server killed in the middle of a download (os._exit on SIGTERM) leaves
                # the download mode on. The packet size and delay are rewritten by every
                # start_stream, so only the download mode needs a reset here.
                with contextlib.suppress(GVCPError):
                    cam.write_register(
                        reg.REG_MEMORY_BUFFER_DOWNLOAD_MODE,
                        int(reg.MemoryBufferDownloadMode.OFF),
                    )
                if self._closed:  # the server shut down while the camera was connecting
                    raise JobCancelledError()
                # Read the state before the session owns the camera, so a failure here
                # cannot leave a connected camera behind a "failed" job.
                self.refresh_snapshot(cam)
            except BaseException:
                with contextlib.suppress(Exception):
                    cam.disconnect()
                raise
            self.camera = cam
            return {"settings": self._snapshot["settings"]}

        return run

    def _wait_for_cooldown(self, cam, job: Job) -> None:
        start = time.monotonic()
        while True:
            job.check_cancel()
            elapsed = time.monotonic() - start
            msg = f"Camera is cooling down ({elapsed:.0f} s)"
            with contextlib.suppress(Exception):
                msg += f", sensor {cam.sensor_temperature('sensor'):.1f} C"
            job.update(min(0.9, elapsed / COOLING_BUDGET_S), msg)
            try:
                cam.wait_until_ready(timeout=10.0, cooling_timeout=COOL_SLICE_S, verbose=False)
                return
            except TimeoutError as exc:
                # The driver words a still-progressing cooldown this way. Anything else
                # (not responding) is a real failure.
                if "still cooling" not in str(exc):
                    raise
                if elapsed > COOLING_BUDGET_S:
                    raise TimeoutError(
                        f"The camera is still cooling after {COOLING_BUDGET_S:.0f} s."
                    ) from exc

    def record_job(
        self, n_sequences: int, duration_s: float | None, frames_per_seq: int | None, pre_moi: int
    ) -> Callable[[Job], dict]:
        def run(job: Job) -> dict:
            cam = self.require_camera()
            # buffer_configure may wipe recordings, and it is not proven that it keeps them.
            # Refuse before touching anything while the camera still holds data.
            held = self.held_sequences(cam)
            if held:
                raise ToolError(self.held_message(held))
            job.update(0.0, "Configuring buffer")
            cam.buffer_configure(
                n_sequences=n_sequences,
                duration=duration_s,
                frames_per_seq=frames_per_seq,
                pre_moi=pre_moi,
                moi_source="software",
            )
            fps = float(cam.frame_rate)
            if frames_per_seq:
                frames = frames_per_seq
            elif duration_s:
                frames = int(duration_s * fps)  # same rule as Camera.buffer_configure
            else:
                frames = 100  # the driver default
            with contextlib.suppress(GVCPError):  # the configured count, as the camera holds it
                frames = int(cam.read_register(reg.REG_MEMORY_BUFFER_SEQ_SIZE)) or frames
            rec_s = frames / fps if fps > 0 else 0.0
            seq_timeout = max(rec_s * 2 + 30.0, 45.0)  # same rule as Camera.buffer_record
            expected = 2.1 + n_sequences * rec_s
            t0 = time.monotonic()

            def tick(label: str) -> None:
                el = time.monotonic() - t0
                job.update(
                    min(0.95, el / expected), f"{label} ({el:.0f} s of about {expected:.0f} s)"
                )

            try:
                cam.buffer_arm(wait_ready=False)
                while cam.buffer_ready_in() > 0:
                    job.check_cancel()
                    tick("Waiting for the ring buffer")
                    time.sleep(min(0.25, max(cam.buffer_ready_in(), 0.01)))
                for i in range(n_sequences):
                    job.check_cancel()
                    if i > 0 and fps > 0:
                        _sleep_cancellable(job, pre_moi / fps)  # refill the pre-trigger window
                    cam.buffer_fire_moi()
                    deadline = time.monotonic() + seq_timeout
                    while True:
                        job.check_cancel()
                        tick(f"Recording sequence {i + 1} of {n_sequences}")
                        try:
                            # Reads the sequence counter only; no register writes.
                            cam._buffer_wait_sequence(i + 1, timeout=0.5, poll_interval=0.25)
                            break
                        except TimeoutError:
                            if time.monotonic() > deadline:
                                raise
            finally:
                # Like Camera.buffer_record: never leave the camera armed. Each step has its
                # own suppress, so one failure does not skip the rest.
                with contextlib.suppress(Exception):
                    cam.write_register(reg.REG_ACQUISITION_STOP, 1)
                with contextlib.suppress(Exception):
                    cam._clear_buffer_arm_state()
                with contextlib.suppress(Exception):
                    time.sleep(0.3)
            seqs = []
            for i in range(n_sequences):
                n = int(cam.buffer_recorded_frames(i))  # safe only after the stop
                moi = None
                with contextlib.suppress(Exception):
                    moi = int(cam.buffer_moi_index(i))
                seqs.append({"sequence": i, "frames": n, "moi_index": moi})
            bad = [s for s in seqs if s["frames"] != frames]
            if bad:
                raise RuntimeError(
                    f"The camera recorded {[s['frames'] for s in seqs]} frames per sequence, "
                    f"not the {frames} configured. The recording is not usable. Run "
                    "buffer_clear with confirm=true and record again."
                )
            return {
                "sequences": seqs,
                "total_frames": sum(s["frames"] for s in seqs),
                "frame_rate_hz": fps,
            }

        return run

    def download_job(self, sequence: int, name: str | None) -> Callable[[Job], dict]:
        def run(job: Job) -> dict:
            cam = self.require_camera()
            out_dir = self.data_dir / "downloads"
            stem = artifacts.new_stem(out_dir, "download", name)
            npy, meta_path = out_dir / f"{stem}.npy", out_dir / f"{stem}.json"
            total = int(cam.buffer_recorded_frames(sequence))  # also selects the sequence
            if total == 0:
                raise ValueError(f"Sequence {sequence} holds no frames. Run buffer_record first.")
            first_id = int(cam.read_register(reg.REG_MEMORY_BUFFER_SEQ_FIRST_FRAME_ID))

            # Recording settings come from the frame headers (filled in at the first slice),
            # not from the camera now: the user may have changed settings since.
            meta: dict = {
                "format": "raw uint16 frames with the 2 Telops header rows, shape (N, H+2, W)",
                "complete": False,
                "state": "running",
                "created_utc": artifacts.utc_iso(),
                "file": npy.name,
                "sequence": sequence,
                "frames_expected": total,
                "frames_written": 0,
                "first_frame_id": first_id,
                "header_rows": cam.HEADER_ROWS,
                "moi_index": None,
                "conversion": (
                    "Per frame, from the header: celsius = raw[2:, :] * 2**data_exp + data_offset "
                    "- 273.15 when calibration_mode is 2 (RT). See docs mcp.rst."
                ),
            }
            with contextlib.suppress(Exception):
                meta["moi_index"] = int(cam.buffer_moi_index(sequence))
            artifacts.write_json(meta_path, meta)

            agg = {
                "slices": 0,
                "n_frames": 0,
                "n_incomplete": 0,
                "n_out_of_order": 0,
                "n_stride_gaps": 0,
                "elapsed_s": 0.0,
                "packet_size_used": None,
                "bitrate_used": None,
            }
            warn: list[str] = []
            n_id_mismatch = 0
            mm = None
            written = 0
            state = "failed"
            t0 = time.monotonic()
            try:
                for lo in range(0, total, SLICE_FRAMES):
                    job.check_cancel()
                    n = min(SLICE_FRAMES, total - lo)
                    # start_frame is a buffer frame ID, not an index; n_frames must not be 0.
                    arr = cam.buffer_download(
                        sequence=sequence,
                        start_frame=first_id + lo,
                        n_frames=n,
                        strip_header=False,
                        convert=False,
                        verbose=False,
                        max_dropped_frames=0,
                    )
                    if arr is None or arr.shape[0] != n:
                        got = 0 if arr is None else arr.shape[0]
                        raise RuntimeError(f"Slice at frame {lo} returned {got} of {n} frames.")
                    if mm is None:
                        need = total * arr[0].nbytes
                        free = shutil.disk_usage(out_dir).free
                        if need * 1.05 > free:
                            raise OSError(
                                f"Need {need / 1e9:.2f} GB, only {free / 1e9:.2f} GB free "
                                f"in {out_dir}."
                            )
                        mm = np.lib.format.open_memmap(
                            npy, mode="w+", dtype=arr.dtype, shape=(total, *arr.shape[1:])
                        )
                        meta["shape"] = list(mm.shape)
                        meta["dtype"] = str(mm.dtype)
                        meta["header"] = _header_info(arr[0])
                        meta.update(_recording_settings(meta["header"], cam.HEADER_ROWS))
                    mm[lo : lo + n] = arr
                    mm.flush()
                    written = lo + n
                    # buffer_download checks frame order inside one call only. Slices are
                    # joined here, so check every frame ID of the slice against the range
                    # that was requested.
                    ids = header_frame_ids(arr).astype(np.int64)
                    bad_ids = int(np.count_nonzero(ids != first_id + lo + np.arange(n)))
                    if bad_ids:
                        n_id_mismatch += bad_ids
                        warn.append(
                            f"slice at {lo}: {bad_ids} of {n} frame IDs differ from the "
                            f"requested range starting at {first_id + lo}"
                        )
                    st = cam.last_download_stats
                    if st is not None:
                        d = asdict(st)
                        agg["slices"] += 1
                        for k in ("n_frames", "n_incomplete", "n_out_of_order", "n_stride_gaps"):
                            agg[k] += int(d[k])
                        agg["elapsed_s"] += float(d["elapsed_s"])
                        agg["packet_size_used"] = d["packet_size_used"]
                        agg["bitrate_used"] = d["bitrate_used"]
                    meta.update(frames_written=written)
                    el = time.monotonic() - t0
                    job.update(
                        written / total, f"Downloaded {written} of {total} frames ({el:.0f} s)"
                    )
                    artifacts.write_json(meta_path, meta)
                state = "done"
            except JobCancelledError:
                state = "cancelled"
                raise
            finally:
                if mm is not None:
                    mm.flush()
                    del mm
                if agg["n_out_of_order"] or agg["n_stride_gaps"]:
                    warn.append(
                        f"{agg['n_out_of_order']} out-of-order and {agg['n_stride_gaps']} "
                        "strided frames inside slices"
                    )
                meta.update(
                    state=state,
                    complete=state == "done",
                    frames_written=written,
                    download_stats=agg,
                    n_frame_id_mismatch=n_id_mismatch,
                    warnings=warn,
                    integrity_ok=(
                        state == "done"
                        and not warn
                        and n_id_mismatch == 0
                        and agg["n_incomplete"] == 0
                        and agg["n_out_of_order"] == 0
                        and agg["n_stride_gaps"] == 0
                    ),
                )
                artifacts.write_json(meta_path, meta)
            return {
                "npy": str(npy),
                "json": str(meta_path),
                "shape": meta["shape"],
                "dtype": "uint16",
                "frames": written,
                "integrity_ok": meta["integrity_ok"],
                "warnings": warn,
            }

        return run


def _sleep_cancellable(job: Job, seconds: float) -> None:
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        job.check_cancel()
        time.sleep(min(0.25, left))


def _header_info(frame: np.ndarray) -> dict:
    """Return the conversion and timing fields of one frame's header."""
    hd = parse_header(np.asarray(frame))
    return {
        "frame_id": hd.frame_id,
        "data_exp": hd.data_exp,
        "data_offset": hd.data_offset,
        "calibration_mode": hd.calibration_mode,
        "exposure_time_us": hd.exposure_time_us,
        "frame_rate_hz": hd.frame_rate_hz,
        "width": hd.width,
        "height": hd.height,
        "timestamp_utc_s": hd.timestamp,
    }


def _recording_settings(header: dict, header_rows: int) -> dict:
    """Return the recording settings as the first frame header states them."""
    try:
        cal = reg.CalibrationMode(header["calibration_mode"]).name
    except ValueError:
        cal = str(header["calibration_mode"])
    return {
        "resolution": [header["width"], header["height"] - header_rows],
        "calibration_mode": cal,
        "integration_time_us": header["exposure_time_us"],
        "frame_rate_hz": header["frame_rate_hz"],
    }
