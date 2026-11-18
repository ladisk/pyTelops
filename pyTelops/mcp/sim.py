"""A small simulated camera for ``pytelops mcp --simulate`` and for the tests.

It implements the part of :class:`pyTelops.Camera` that the MCP server uses and
mimics the behaviours that matter to it: a cooldown that ends in
``TimeoutError`` while the camera stays marked connected, frame IDs that start
above zero, ``GVCPError`` from register reads while recording, and
``start_frame``/``n_frames`` slicing in the download. Faults can be injected.
"""

from __future__ import annotations

import struct
import threading
import time
import warnings
from collections import Counter

import numpy as np
from pyGigEVision import GVCPError

from .. import registers as reg
from ..errors import DownloadStats, FrameIntegrityError

_FIRST_ID = 1000  # buffer frame IDs do not start at 0
_SEQ_STRIDE = 100_000


class SimWorld:
    """State that outlives one ``SimCamera`` object, like the physical camera."""

    def __init__(self, cooldown_s: float = 0.0):
        self.cooldown_s = cooldown_s
        self.ready_at: float | None = None
        self.width, self.height = 320, 256
        self.offset = (0, 0)
        self.integration_us = 500.0
        self.frame_rate = 100.0
        self.cal = reg.CalibrationMode.RT
        self.buffer_mode = False  # REG_MEMORY_BUFFER_MODE
        self.seq_frames: dict[int, int] = {}
        self.calls: Counter = Counter()
        self.log: list[str] = []  # order of setting writes
        self.lock = threading.Lock()


class SimCamera:
    """Simulated camera. Parameter names match :class:`pyTelops.Camera`."""

    HEADER_ROWS = 2
    RING_WARMUP_S = 0.05

    def __init__(
        self,
        ip: str | None = None,
        local_ip: str | None = None,
        timeout: float = 2.0,
        *,
        world: SimWorld | None = None,
        connect_fault: str | None = None,
        download_fault: str | None = None,
        slice_delay_s: float = 0.0,
    ) -> None:
        self.world = world or SimWorld()
        self._camera_ip = ip or "192.0.2.10"
        self.connect_fault = connect_fault  # "access_denied" or "timeout"
        self.download_fault = download_fault  # "integrity", "wrong_ids" or "out_of_order"
        self.slice_delay_s = slice_delay_s
        self._connected = False
        self.defaults_applied = False
        self.last_download_stats = None
        self.download_calls: list[tuple[int, int]] = []
        self._cfg: dict = {}
        self._armed_at: float | None = None
        self._ready_at: float | None = None
        self._moi_times: list[float] = []
        self._selector = 0

    @property
    def calls(self) -> Counter:
        return self.world.calls

    # ----- connection -------------------------------------------------
    def connect(
        self, timeout: float = 10.0, cooling_timeout: float = 600.0, force_control: bool = True
    ) -> None:
        self.calls["connect"] += 1
        if self._connected:
            return
        if self.connect_fault == "access_denied":
            raise GVCPError("Could not take CCP control", 0x8006)
        if self.connect_fault == "timeout":
            raise TimeoutError("Camera not ready after 10s (not responding); check power.")
        w = self.world
        if w.cooldown_s > 0 and w.ready_at is None:
            w.ready_at = time.monotonic() + w.cooldown_s
        self._connected = True
        print("Discovered a simulated camera", flush=True)  # real connect prints too
        if w.ready_at is not None and time.monotonic() < w.ready_at:
            self.wait_until_ready(timeout=timeout, cooling_timeout=cooling_timeout)
        self.defaults_applied = True

    def wait_until_ready(
        self, timeout: float = 10.0, cooling_timeout: float = 600.0, verbose: bool = True
    ) -> None:
        self._check_connected()
        ready_at = self.world.ready_at
        if ready_at is None or time.monotonic() >= ready_at:
            return
        if verbose:
            print("Waiting: Cooling down", flush=True)
        time.sleep(max(0.0, min(cooling_timeout, ready_at - time.monotonic())))
        if time.monotonic() < ready_at:
            raise TimeoutError(
                "Camera not ready after 1s (Cooling down); still cooling -- raise cooling_timeout."
            )

    def disconnect(self) -> None:
        self.calls["disconnect"] += 1
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def camera_ip(self) -> str | None:
        return self._camera_ip

    def _check_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("Camera is not connected. Call connect() first.")

    # ----- settings ---------------------------------------------------
    @property
    def resolution(self) -> tuple[int, int]:
        self._check_connected()
        return self.world.width, self.world.height

    @resolution.setter
    def resolution(self, wh: tuple[int, int]) -> None:
        self._check_connected()
        w, h = wh
        if not (64 <= w <= 320 and w % 64 == 0 and 4 <= h <= 256 and h % 4 == 0):
            raise ValueError(f"Invalid resolution {w}x{h}")
        ox, oy = self.world.offset
        if ox + w > 320 or oy + h > 256:
            # The camera refuses a window that leaves the sensor (assumed, like the
            # offset setter, which checks the same rule).
            raise GVCPError(f"window {w}x{h} at offset ({ox}, {oy}) exceeds the sensor", 0x8FFF)
        self.world.width, self.world.height = w, h
        self.calls["set_resolution"] += 1
        self.world.log.append("set_resolution")
        self._clamp_fps()

    @property
    def roi_offset(self) -> tuple[int, int]:
        self._check_connected()
        return self.world.offset

    @roi_offset.setter
    def roi_offset(self, xy: tuple[int, int]) -> None:
        self._check_connected()
        x, y = int(xy[0]), int(xy[1])
        if x < 0 or x % 64 != 0:
            raise ValueError(f"roi_offset x={x} is invalid. Must be a multiple of 64.")
        if y < 0 or y % 4 != 0:
            raise ValueError(f"roi_offset y={y} is invalid. Must be a multiple of 4.")
        w, h = self.world.width, self.world.height
        if x + w > 320 or y + h > 256:
            raise ValueError(f"roi_offset ({x}, {y}) + size {w}x{h} exceeds the sensor")
        self.world.offset = (x, y)
        self.calls["set_roi_offset"] += 1
        self.world.log.append("set_roi_offset")

    @property
    def integration_time(self) -> float:
        self._check_connected()
        return self.world.integration_us

    @integration_time.setter
    def integration_time(self, us: float) -> None:
        self._check_connected()
        self.world.integration_us = min(float(us), 10000.0)
        self.calls["set_integration_time"] += 1
        self.world.log.append("set_integration_time")

    @property
    def frame_rate_max(self) -> float:
        self._check_connected()
        w = self.world
        return 3100.0 * (320 * 256) / (w.width * w.height)

    @property
    def frame_rate(self) -> float:
        self._check_connected()
        return self.world.frame_rate

    @frame_rate.setter
    def frame_rate(self, hz: float) -> None:
        self._check_connected()
        self.world.frame_rate = float(hz)
        self.calls["set_frame_rate"] += 1
        self.world.log.append("set_frame_rate")
        self._clamp_fps()

    def _clamp_fps(self) -> None:
        mx = self.frame_rate_max
        if self.world.frame_rate > mx:
            self.world.frame_rate = mx
            warnings.warn(f"frame rate clamped to {mx:.1f} Hz", UserWarning, stacklevel=3)

    @property
    def calibration_mode(self) -> reg.CalibrationMode:
        self._check_connected()
        return self.world.cal

    @calibration_mode.setter
    def calibration_mode(self, mode) -> None:
        self._check_connected()
        if isinstance(mode, str):
            try:
                mode = reg.CalibrationMode[mode.upper()]
            except KeyError:
                raise ValueError(f"Unknown calibration mode {mode!r}") from None
        self.world.cal = reg.CalibrationMode(mode)
        self.calls["set_calibration_mode"] += 1
        self.world.log.append("set_calibration_mode")

    @property
    def info(self) -> dict:
        self._check_connected()
        w = self.world
        return {
            "ip": self._camera_ip,
            "width": w.width,
            "height": w.height,
            "integration_time_us": w.integration_us,
            "frame_rate_hz": w.frame_rate,
            "frame_rate_max_hz": self.frame_rate_max,
            "calibration": w.cal.name,
            "buffer_mode": "ON" if w.buffer_mode else "OFF",
            "temperature_c": 80.0,
            "roi_offset": w.offset,
        }

    @property
    def tdc_status(self) -> int:
        self._check_connected()
        return 0

    def sensor_temperature(self, location="sensor") -> float:
        self._check_connected()
        return 80.0

    # ----- frames -----------------------------------------------------
    def _raw(self, n: int, first_id: int) -> np.ndarray:
        w = self.world
        h = w.height + self.HEADER_ROWS
        out = np.zeros((n, h, w.width), np.uint16)
        yy, xx = np.mgrid[0 : w.height, 0 : w.width]
        base = (20000 + 20 * xx + 10 * yy).astype(np.uint16)
        for i in range(n):
            out[i, self.HEADER_ROWS :, :] = base + (first_id + i) % 50
            hdr = np.zeros(256, np.uint8)
            hdr[0:2] = np.frombuffer(b"TC", np.uint8)
            hdr[2], hdr[3] = 9, 12  # header version 12.9
            struct.pack_into("<H", hdr, 4, 256)
            struct.pack_into("<I", hdr, 8, first_id + i)
            struct.pack_into("<f", hdr, 12, 273.15)
            struct.pack_into("<b", hdr, 16, -8)
            struct.pack_into("<I", hdr, 24, int(w.integration_us * 100))
            hdr[28] = 2 if w.cal is reg.CalibrationMode.RT else 255
            if w.cal is not reg.CalibrationMode.RT:  # NUC/RAW frames carry no conversion
                struct.pack_into("<f", hdr, 12, 0.0)
                struct.pack_into("<b", hdr, 16, 0)
            struct.pack_into("<HH", hdr, 32, w.width, h)
            struct.pack_into("<I", hdr, 44, int(w.frame_rate * 1000))
            struct.pack_into("<I", hdr, 100, 1_790_000_000)
            struct.pack_into("<I", hdr, 104, int(i * 1e7 / w.frame_rate))
            out[i, : self.HEADER_ROWS, :].reshape(-1)[:128] = hdr.view(np.uint16)
        return out

    def _finish(self, raw: np.ndarray, strip_header: bool, convert: bool) -> np.ndarray:
        if convert:
            if self.world.cal is not reg.CalibrationMode.RT:
                return raw[:, self.HEADER_ROWS :, :]  # like the driver: counts, headers stripped
            return raw[:, self.HEADER_ROWS :, :].astype(np.float32) * 2.0**-8
        return raw[:, self.HEADER_ROWS :, :] if strip_header else raw

    def _check_not_buffer_mode(self) -> None:
        if self.world.buffer_mode:
            raise GVCPError("live acquisition is rejected while buffer mode is on", 0x8007)

    def grab(self, timeout: float = 5.0, strip_header: bool = True, convert: bool = True):
        self._check_connected()
        self._check_not_buffer_mode()
        self.calls["grab"] += 1
        return self._finish(self._raw(1, 0), strip_header, convert)[0]

    def acquire(
        self, n_frames: int, timeout: float = 30.0, strip_header: bool = True, convert: bool = True
    ):
        self._check_connected()
        self._check_not_buffer_mode()
        self.calls["acquire"] += 1
        return self._finish(self._raw(n_frames, 0), strip_header, convert)

    def nuc(self, mode="black_body", blackbody_temp: float | None = None, timeout: float = 60.0):
        self._check_connected()
        self.calls["nuc"] += 1

    # ----- memory buffer ----------------------------------------------
    def buffer_configure(
        self,
        n_sequences: int = 1,
        duration: float | None = None,
        frames_per_seq: int | None = None,
        pre_moi: int = 0,
        moi_source="software",
    ) -> None:
        self._check_connected()
        self.calls["buffer_configure"] += 1
        if duration is not None and frames_per_seq is not None:
            raise ValueError("Give duration or frames_per_seq, not both.")
        n = frames_per_seq or (round(duration * self.frame_rate) if duration else 100)
        self._cfg = {"n_seq": n_sequences, "frames": max(1, int(n)), "pre_moi": pre_moi}
        # Safe assumption for the tests: configure keeps recordings. The real camera is
        # unproven, so the server must not rely on it either way.
        self.world.buffer_mode = True
        self._moi_times = []
        self._armed_at = None

    def _recording(self) -> bool:
        return self._armed_at is not None and self._seq_done() < len(self._moi_times)

    def _seq_done(self) -> int:
        dur = self._cfg.get("frames", 0) / self.frame_rate
        now = time.monotonic()
        return sum(1 for t in self._moi_times if now >= t + dur)

    def buffer_arm(self, wait_ready: bool = True) -> None:
        self._check_connected()
        self.calls["buffer_arm"] += 1
        self._armed_at = time.monotonic()
        self._ready_at = self._armed_at + self.RING_WARMUP_S
        self._moi_times = []
        if wait_ready:
            time.sleep(self.RING_WARMUP_S)

    def buffer_ready_in(self) -> float:
        self._check_connected()
        if self._ready_at is None:
            return 0.0
        return max(0.0, self._ready_at - time.monotonic())

    def buffer_fire_moi(self) -> None:
        self._check_connected()
        self.calls["buffer_fire_moi"] += 1
        if self._armed_at is None:
            raise RuntimeError("The camera is not armed for buffer recording.")
        self._moi_times.append(time.monotonic())

    def _buffer_wait_sequence(self, target_count: int, timeout: float = 30.0, poll_interval=0.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._seq_done() >= target_count:
                return
            time.sleep(min(poll_interval, 0.02))
        raise TimeoutError(f"Sequence count did not reach {target_count} within {timeout:.0f}s")

    def _clear_buffer_arm_state(self) -> None:
        self._ready_at = None

    def write_register(self, addr: int, value: int) -> None:
        self._check_connected()
        if addr == reg.REG_ACQUISITION_STOP:
            self.calls["acquisition_stop"] += 1
            done = self._seq_done()
            # Stopping mid-sequence aborts it; completed sequences stay.
            for i in range(done):
                self.world.seq_frames[i] = self._cfg["frames"]
            self._armed_at = None
            self._moi_times = []
        elif addr == reg.REG_MEMORY_BUFFER_SEQ_SELECTOR:
            self._selector = value
        elif addr == reg.REG_MEMORY_BUFFER_MODE:
            self.world.buffer_mode = bool(value)
        elif addr == reg.REG_MEMORY_BUFFER_DOWNLOAD_MODE:
            self.calls["download_mode_write"] += 1

    def read_register(self, addr: int) -> int:
        self._check_connected()
        if addr == reg.REG_MEMORY_BUFFER_SEQ_FIRST_FRAME_ID:
            return _FIRST_ID + self._selector * _SEQ_STRIDE
        if addr == reg.REG_MEMORY_BUFFER_SEQ_RECORDED_SIZE:
            return self.world.seq_frames.get(self._selector, 0)
        if addr == reg.REG_MEMORY_BUFFER_MODE:
            return int(self.world.buffer_mode)
        if addr == reg.REG_MEMORY_BUFFER_SEQ_COUNT:
            return self._seq_done() if self._armed_at is not None else len(self.world.seq_frames)
        if addr == reg.REG_MEMORY_BUFFER_SEQ_SIZE:
            return int(self._cfg.get("frames", 0))
        raise GVCPError(f"register {addr:#x} not simulated", 0x8003)

    def buffer_recorded_frames(self, sequence: int = 0) -> int:
        self._check_connected()
        self.calls["buffer_recorded_frames"] += 1
        if self._recording():
            raise GVCPError("rejected while recording", 0x8007)
        self._selector = sequence
        return self.world.seq_frames.get(sequence, 0)

    def buffer_moi_index(self, sequence: int = 0) -> int:
        self._check_connected()
        return int(self._cfg.get("pre_moi", 0))

    def buffer_status(self) -> reg.MemoryBufferStatus:
        self._check_connected()
        self.calls["buffer_status"] += 1
        if self._recording():
            return reg.MemoryBufferStatus.RECORDING
        return (
            reg.MemoryBufferStatus.HOLDING if self.world.seq_frames else reg.MemoryBufferStatus.IDLE
        )

    def buffer_info(self) -> dict:
        self._check_connected()
        self.calls["buffer_info"] += 1
        n = self._cfg.get("n_seq", 1)
        return {
            "status": self.buffer_status().name,
            "n_sequences": n,
            "recorded": [self.world.seq_frames.get(i, 0) for i in range(n)],
            "total_bytes": 16 * 2**30,
            "free_bytes": 16 * 2**30 - sum(self.world.seq_frames.values()) * 163_840,
        }

    def buffer_clear(self) -> None:
        self._check_connected()
        self.calls["buffer_clear"] += 1
        self.world.seq_frames = {}
        # Like the driver: clear wipes the partition and re-applies the last configure,
        # which turns buffer mode on.
        self.world.buffer_mode = bool(self._cfg)

    def buffer_download(
        self,
        sequence: int = 0,
        start_frame: int | None = None,
        n_frames: int = 0,
        timeout: float = 0,
        bitrate_mbps: float | None = None,
        packet_size: int | None = None,
        strip_header: bool = True,
        convert: bool = True,
        verbose: bool = True,
        max_dropped_frames: int = 0,
        retries: int = 6,
        resend: bool = False,
        verify_order: bool = True,
        order_tolerance: float = 0.05,
        chunk_size: int = 1000,
        return_headers: bool = False,
    ):
        self._check_connected()
        self.calls["buffer_download"] += 1
        self.last_download_stats = None
        recorded = self.world.seq_frames.get(sequence, 0)
        if n_frames == 0:
            n_frames = recorded
        if n_frames == 0:
            return None
        first = _FIRST_ID + sequence * _SEQ_STRIDE
        if start_frame is None:
            start_frame = first
        self.download_calls.append((start_frame, n_frames))
        time.sleep(self.slice_delay_s)
        if self.download_fault == "integrity":
            stats = DownloadStats(n_frames=n_frames - 3, n_incomplete=3)
            self.last_download_stats = stats
            raise FrameIntegrityError("3 frames could not be recovered", stats=stats)
        self.last_download_stats = DownloadStats(
            n_frames=n_frames, elapsed_s=0.01, packet_size_used=1500, bitrate_used=1000.0
        )
        if self.download_fault == "out_of_order":  # tolerated by the driver, still a warning sign
            self.last_download_stats.n_out_of_order = 2
        shift = 5 if self.download_fault == "wrong_ids" else 0  # frames of another range
        return self._finish(self._raw(n_frames, start_frame + shift), strip_header, convert)
