"""
High-level Telops camera driver.

Provides a clean, Pythonic interface to Telops FAST-series thermal cameras
over GigE Vision. Handles discovery, streaming, buffer operations, and
camera configuration.

Usage:
    from pyTelops import Camera

    with Camera() as cam:
        cam.exposure = 50.0
        frame = cam.grab()
"""

import socket
import struct
import time
from typing import Optional

import numpy as np

from .gvcp import GVCPClient, GVCPError, REG_HEARTBEAT_TIMEOUT
from .gvsp import GVSPReceiver
from . import registers as reg

# --- Enum string resolution ---
_ENUM_ALIASES = {
    reg.CalibrationMode: {
        "raw": reg.CalibrationMode.RAW, "raw0": reg.CalibrationMode.RAW0,
        "nuc": reg.CalibrationMode.NUC, "rt": reg.CalibrationMode.RT,
        "ibr": reg.CalibrationMode.IBR, "ibi": reg.CalibrationMode.IBI,
    },
    reg.ExposureAuto: {
        "off": reg.ExposureAuto.OFF, "once": reg.ExposureAuto.ONCE,
        "continuous": reg.ExposureAuto.CONTINUOUS,
    },
    reg.TriggerSource: {
        "software": reg.TriggerSource.SOFTWARE,
        "external": reg.TriggerSource.EXTERNAL_SIGNAL,
    },
    reg.TriggerActivation: {
        "rising": reg.TriggerActivation.RISING_EDGE,
        "falling": reg.TriggerActivation.FALLING_EDGE,
        "any": reg.TriggerActivation.ANY_EDGE,
    },
    reg.TriggerSelector: {
        "acquisition_start": reg.TriggerSelector.ACQUISITION_START,
        "flagging": reg.TriggerSelector.FLAGGING,
        "gating": reg.TriggerSelector.GATING,
    },
    reg.MemoryBufferMOISource: {
        "software": reg.MemoryBufferMOISource.SOFTWARE,
        "external": reg.MemoryBufferMOISource.EXTERNAL_SIGNAL,
        "acquisition_started": reg.MemoryBufferMOISource.ACQUISITION_STARTED,
        "none": reg.MemoryBufferMOISource.NONE,
    },
}


def _resolve_enum(value, enum_cls):
    """Resolve a string or enum value to the enum type."""
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, int):
        return enum_cls(value)
    if isinstance(value, str):
        aliases = _ENUM_ALIASES.get(enum_cls, {})
        key = value.lower().strip()
        if key in aliases:
            return aliases[key]
        # Try matching enum member name
        for member in enum_cls:
            if member.name.lower() == key:
                return member
        valid = list(aliases.keys()) + [m.name for m in enum_cls]
        raise ValueError(f"Unknown {enum_cls.__name__}: {value!r}. "
                         f"Valid: {valid}")
    raise TypeError(f"Expected {enum_cls.__name__}, str, or int, "
                    f"got {type(value).__name__}")


def discover(interface_ip: str = "", timeout: float = 2.0) -> list[dict]:
    """Discover Telops cameras on the network.

    Sends a GVCP broadcast and collects responses from all GigE Vision
    cameras. If no interface_ip is given, tries the link-local interface
    first, then broadcasts on all interfaces.

    Args:
        interface_ip: Local IP to bind to (empty = auto-detect).
        timeout: Seconds to wait for responses.

    Returns:
        List of dicts with keys: ip, manufacturer, model,
        device_version, serial, user_name.
    """
    if interface_ip:
        return GVCPClient.discover(interface_ip, timeout)

    # Try link-local first
    local_ip = _find_link_local_ip()
    if local_ip:
        cameras = GVCPClient.discover(local_ip, timeout)
        if cameras:
            return cameras

    # Fallback: broadcast on all interfaces
    return GVCPClient.discover("", timeout)


def _find_link_local_ip() -> Optional[str]:
    """Find a local link-local (169.254.x.x) interface IP."""
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("169.254."):
                return ip
    except OSError:
        pass
    return None


def _find_local_ip_for(camera_ip: str) -> str:
    """Determine which local IP can reach a given camera IP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((camera_ip, 3956))
        return s.getsockname()[0]
    finally:
        s.close()


class Camera:
    """Telops thermal camera over GigE Vision.

    Control via GVCP (register read/write), streaming via GVSP.
    Supports live acquisition, internal memory buffer, trigger
    configuration, and GUI viewer.

    Args:
        ip: Camera IP address. None = auto-discover first camera.
        local_ip: Local network interface IP. None = auto-detect.
        timeout: UDP timeout in seconds.

    Examples:
        Auto-discover and grab a frame::

            with Camera() as cam:
                frame = cam.grab()

        Connect to a specific camera::

            cam = Camera(ip="169.254.67.34")
            cam.connect()
            cam.exposure = 100.0
            frames = cam.acquire(50)
            cam.disconnect()

        Buffer recording::

            with Camera() as cam:
                cam.buffer_configure(frames_per_seq=1000)
                cam.buffer_arm()
                cam.buffer_fire_moi()
                data = cam.buffer_download()
    """

    # Number of metadata rows embedded in each frame by Telops cameras
    HEADER_ROWS = 2

    # Class-level registry of active Camera instances, keyed by camera IP.
    # Used to forcibly disconnect a stale instance when a new Camera
    # connects to the same camera (e.g., after a kernel restart or when
    # the user forgot to disconnect).
    _active_cameras: dict[str, "Camera"] = {}

    def __init__(self, ip: Optional[str] = None,
                 local_ip: Optional[str] = None,
                 timeout: float = 2.0):
        self._camera_ip = ip
        self._local_ip = local_ip or ""
        self._timeout = timeout

        self._gvcp: Optional[GVCPClient] = None
        self._gvsp: Optional[GVSPReceiver] = None
        self._streaming = False
        self._connected = False
        self._buffer_n_sequences = 1
        self._buffer_next_sequence = 0

    def __repr__(self) -> str:
        status = "connected" if self._connected else "disconnected"
        ip = self._camera_ip or "unknown"
        return f"Camera({ip}, {status})"

    def __del__(self):
        try:
            self.disconnect()
        except Exception:
            pass

    # ==========================================================
    # Context Manager
    # ==========================================================

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    # ==========================================================
    # Connection
    # ==========================================================

    def connect(self) -> None:
        """Discover camera (if needed) and establish GVCP control.

        If a previous Camera instance in the same process is still
        connected to this camera, it is automatically disconnected first.
        If a stale session from another process holds CCP, we poll until
        the camera's heartbeat timeout expires (up to ~15 s).

        Raises:
            RuntimeError: If no camera is found.
            GVCPError: If GVCP handshake fails.
        """
        if self._connected:
            return

        # Auto-discover
        if self._camera_ip is None:
            cameras = discover(self._local_ip, self._timeout)
            if not cameras:
                raise RuntimeError(
                    "No Telops camera found. Check:\n"
                    "  1. Camera is powered on\n"
                    "  2. Ethernet cable is connected\n"
                    "  3. No other software has GVCP control\n"
                    "  4. Firewall allows UDP for this python.exe")
            self._camera_ip = cameras[0]["ip"]
            print(f"Discovered: {cameras[0].get('manufacturer', '')} "
                  f"{cameras[0].get('model', '')} at {self._camera_ip}")

        # If there's an existing Camera in this process connected to the
        # same camera IP, disconnect it first (handles "forgot to disconnect"
        # and "kernel restart" scenarios within the same process).
        old = Camera._active_cameras.get(self._camera_ip)
        if old is not None and old is not self and old._connected:
            print(f"Disconnecting previous Camera instance for "
                  f"{self._camera_ip}...")
            try:
                old.disconnect()
            except Exception:
                pass

        # Auto-detect local IP if not specified
        if not self._local_ip:
            self._local_ip = _find_local_ip_for(self._camera_ip)

        # GVCP connection
        self._gvcp = GVCPClient(self._camera_ip, self._local_ip, self._timeout)
        self._gvcp.connect()

        # Reset heartbeat timeout
        try:
            self._gvcp.write_reg(REG_HEARTBEAT_TIMEOUT, 3000)
        except GVCPError:
            pass

        # Stop any stale acquisition left over from a previous session
        # (e.g., crash without proper disconnect)
        try:
            self._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)
        except GVCPError:
            pass

        # Clear stream destination (stop any stale streaming)
        try:
            self._gvcp.write_reg(reg.REG_SC_HOST_PORT, 0)
        except GVCPError:
            pass

        # Prepare GVSP receiver
        self._gvsp = GVSPReceiver(self._local_ip, gvcp_client=self._gvcp)

        self._connected = True
        Camera._active_cameras[self._camera_ip] = self

    def disconnect(self) -> None:
        """Stop streaming, release GVCP control, close sockets."""
        if not self._connected:
            return

        if self._streaming:
            self.stop_stream()

        if self._gvsp:
            self._gvsp.close()
            self._gvsp = None

        if self._gvcp:
            self._gvcp.disconnect()
            self._gvcp = None

        self._connected = False

        # Remove from active registry
        if (self._camera_ip and
                Camera._active_cameras.get(self._camera_ip) is self):
            del Camera._active_cameras[self._camera_ip]

    @property
    def is_connected(self) -> bool:
        """Whether the camera is connected."""
        return self._connected

    @property
    def is_streaming(self) -> bool:
        """Whether GVSP streaming is active."""
        return self._streaming

    @property
    def camera_ip(self) -> Optional[str]:
        """Camera IP address (None if not yet discovered)."""
        return self._camera_ip

    # ==========================================================
    # Camera Configuration (properties)
    # ==========================================================

    def _check_connected(self):
        if not self._connected:
            raise RuntimeError("Camera not connected. Call connect() first.")

    @property
    def exposure(self) -> float:
        """Exposure time in microseconds."""
        self._check_connected()
        return self._gvcp.read_float(reg.REG_EXPOSURE_TIME)

    @exposure.setter
    def exposure(self, us: float):
        self._check_connected()
        # Disable AEC if active (it locks ExposureTime register)
        aec = self._gvcp.read_reg(reg.REG_EXPOSURE_AUTO)
        if aec != reg.ExposureAuto.OFF:
            self._gvcp.write_reg(reg.REG_EXPOSURE_AUTO, reg.ExposureAuto.OFF)
        self._gvcp.write_float(reg.REG_EXPOSURE_TIME, us)

    @property
    def exposure_auto(self) -> reg.ExposureAuto:
        """Auto exposure control mode (OFF, ONCE, CONTINUOUS)."""
        self._check_connected()
        return reg.ExposureAuto(self._gvcp.read_reg(reg.REG_EXPOSURE_AUTO))

    @exposure_auto.setter
    def exposure_auto(self, mode):
        self._check_connected()
        self._gvcp.write_reg(reg.REG_EXPOSURE_AUTO,
                             int(_resolve_enum(mode, reg.ExposureAuto)))

    @property
    def frame_rate(self) -> float:
        """Acquisition frame rate in Hz."""
        self._check_connected()
        return self._gvcp.read_float(reg.REG_ACQUISITION_FRAME_RATE)

    @frame_rate.setter
    def frame_rate(self, hz: float):
        self._check_connected()
        self._gvcp.write_float(reg.REG_ACQUISITION_FRAME_RATE, hz)

    @property
    def calibration_mode(self) -> reg.CalibrationMode:
        """Calibration mode (RAW, NUC, RT, IBR, IBI)."""
        self._check_connected()
        return reg.CalibrationMode(self._gvcp.read_reg(reg.REG_CALIBRATION_MODE))

    @calibration_mode.setter
    def calibration_mode(self, mode):
        self._check_connected()
        self._gvcp.write_reg(reg.REG_CALIBRATION_MODE,
                             int(_resolve_enum(mode, reg.CalibrationMode)))

    @property
    def resolution(self) -> tuple[int, int]:
        """Image resolution as (width, height)."""
        self._check_connected()
        w = self._gvcp.read_reg(reg.REG_WIDTH)
        h = self._gvcp.read_reg(reg.REG_HEIGHT)
        return w, h

    @resolution.setter
    def resolution(self, wh: tuple[int, int]):
        self._check_connected()
        self._gvcp.write_reg(reg.REG_WIDTH, wh[0])
        self._gvcp.write_reg(reg.REG_HEIGHT, wh[1])

    @property
    def temperature(self) -> float:
        """Camera sensor temperature in Celsius (read-only)."""
        self._check_connected()
        return self._gvcp.read_float(reg.REG_DEVICE_TEMPERATURE)

    @property
    def info(self) -> dict:
        """Current camera configuration as a dict."""
        self._check_connected()
        return {
            "ip": self._camera_ip,
            "width": self._gvcp.read_reg(reg.REG_WIDTH),
            "height": self._gvcp.read_reg(reg.REG_HEIGHT),
            "exposure_us": self._gvcp.read_float(reg.REG_EXPOSURE_TIME),
            "exposure_auto": reg.ExposureAuto(
                self._gvcp.read_reg(reg.REG_EXPOSURE_AUTO)).name,
            "frame_rate_hz": self._gvcp.read_float(
                reg.REG_ACQUISITION_FRAME_RATE),
            "calibration": reg.CalibrationMode(
                self._gvcp.read_reg(reg.REG_CALIBRATION_MODE)).name,
            "trigger_mode": reg.TriggerMode(
                self._gvcp.read_reg(reg.REG_TRIGGER_MODE)).name,
            "power_state": reg.DevicePowerState(
                self._gvcp.read_reg(reg.REG_DEVICE_POWER_STATE)).name,
            "temperature_c": self._gvcp.read_float(reg.REG_DEVICE_TEMPERATURE),
            "buffer_mode": reg.MemoryBufferMode(
                self._gvcp.read_reg(reg.REG_MEMORY_BUFFER_MODE)).name,
        }

    @property
    def state(self) -> str:
        """Camera state: disconnected, connected, streaming, standby, error."""
        if not self._connected or not self._gvcp:
            return "disconnected"
        if self._streaming:
            return "streaming"
        try:
            power = self._gvcp.read_reg(reg.REG_DEVICE_POWER_STATE)
            not_ready = self._gvcp.read_reg(reg.REG_DEVICE_NOT_READY)
            if power != reg.DevicePowerState.ON:
                return "standby"
            if not_ready:
                return "not_ready"
            return "connected"
        except GVCPError:
            return "error"

    # ==========================================================
    # Streaming
    # ==========================================================

    def start_stream(self) -> None:
        """Configure stream channel and start GVSP receiver."""
        self._check_connected()
        if self._streaming:
            return

        # Clamp packet size to standard MTU
        target_pkt_size = 1500
        pkt_reg = self._gvcp.read_reg(reg.REG_SC_PACKET_SIZE)
        current_size = pkt_reg & 0xFFFF
        if current_size != target_pkt_size:
            flags = pkt_reg & 0xFFFF0000
            self._gvcp.write_reg(reg.REG_SC_PACKET_SIZE,
                                 flags | target_pkt_size)

        self._gvsp._packet_data_size = target_pkt_size - 8

        # Minimize inter-packet delay
        try:
            delay = self._gvcp.read_reg(reg.REG_SC_PACKET_DELAY)
            if delay != 0:
                self._gvcp.write_reg(reg.REG_SC_PACKET_DELAY, 0)
        except GVCPError:
            pass

        # Tell camera where to send stream data
        ip_bytes = socket.inet_aton(
            self._gvsp._sock.getsockname()[0] or self._local_ip)
        ip_int = struct.unpack(">I", ip_bytes)[0]

        self._gvcp.write_reg(reg.REG_SC_DEST_ADDR, ip_int)
        self._gvcp.write_reg(reg.REG_SC_HOST_PORT, self._gvsp.port)

        self._gvsp.start()
        self._streaming = True

    def stop_stream(self) -> None:
        """Stop acquisition and GVSP receiver."""
        if not self._streaming:
            return

        try:
            self._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)
        except GVCPError:
            pass

        try:
            self._gvcp.write_reg(reg.REG_SC_HOST_PORT, 0)
        except GVCPError:
            pass

        self._gvsp.stop()
        self._streaming = False

    # ==========================================================
    # Frame Acquisition
    # ==========================================================

    def _strip_headers(self, arr: np.ndarray) -> np.ndarray:
        """Strip Telops header rows from a frame or batch of frames."""
        if self.HEADER_ROWS == 0:
            return arr
        if arr.ndim == 2:
            return arr[self.HEADER_ROWS:, :]
        elif arr.ndim == 3:
            return arr[:, self.HEADER_ROWS:, :]
        return arr

    def grab(self, timeout: float = 5.0,
             strip_header: bool = True) -> Optional[np.ndarray]:
        """Grab a single frame.

        Starts streaming if not already active, grabs one frame.

        Args:
            timeout: Seconds to wait for a frame.
            strip_header: Remove Telops metadata rows (default True).

        Returns:
            2D numpy array (H, W) of uint16 pixel values, or None on timeout.
        """
        self._check_connected()
        was_streaming = self._streaming
        if not self._streaming:
            self.start_stream()
            self._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)

        frame = self._gvsp.get_frame(timeout=timeout)

        if not was_streaming:
            self.stop_stream()

        if frame is not None and strip_header:
            frame = self._strip_headers(frame)
        return frame

    def acquire(self, n_frames: int, timeout: float = 30.0,
                strip_header: bool = True) -> Optional[np.ndarray]:
        """Acquire multiple frames via live streaming.

        Args:
            n_frames: Number of frames to capture.
            timeout: Total timeout in seconds.
            strip_header: Remove Telops metadata rows (default True).

        Returns:
            3D numpy array (N, H, W) or None if no frames captured.
        """
        self._check_connected()
        was_streaming = self._streaming
        if not self._streaming:
            self.start_stream()
            self._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)

        frames = []
        deadline = time.monotonic() + timeout

        for i in range(n_frames):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            result = self._gvsp.get_frame(timeout=remaining)
            if result is not None:
                frames.append(result)

        if not was_streaming:
            self.stop_stream()

        if not frames:
            return None
        result = np.stack(frames)
        if strip_header:
            result = self._strip_headers(result)
        return result

    # ==========================================================
    # Trigger
    # ==========================================================

    def configure_trigger(self, source="external", activation="rising",
                          selector="acquisition_start",
                          enabled: bool = True) -> None:
        """Configure external trigger.

        Args:
            source: "software" or "external" (or TriggerSource enum).
            activation: "rising", "falling", "any" (or TriggerActivation enum).
            selector: "acquisition_start", "flagging", "gating" (or enum).
            enabled: Enable or disable trigger mode.
        """
        self._check_connected()
        self._gvcp.write_reg(reg.REG_TRIGGER_SELECTOR,
                             int(_resolve_enum(selector, reg.TriggerSelector)))
        self._gvcp.write_reg(reg.REG_TRIGGER_SOURCE,
                             int(_resolve_enum(source, reg.TriggerSource)))
        self._gvcp.write_reg(reg.REG_TRIGGER_ACTIVATION,
                             int(_resolve_enum(activation, reg.TriggerActivation)))
        self._gvcp.write_reg(reg.REG_TRIGGER_MODE,
                             int(reg.TriggerMode.ON if enabled
                                 else reg.TriggerMode.OFF))

    def software_trigger(self) -> None:
        """Send a software trigger command."""
        self._check_connected()
        self._gvcp.write_reg(reg.REG_TRIGGER_SOFTWARE, 1)

    # ==========================================================
    # Memory Buffer (16GB onboard)
    # ==========================================================

    def buffer_configure(self, n_sequences: int = 1,
                         frames_per_seq: int = 100, pre_moi: int = 0,
                         moi_source="software") -> None:
        """Configure the internal memory buffer for recording.

        The camera has a 16GB ring buffer that records at full sensor speed
        (up to 3100 fps), independent of the Ethernet link. The buffer must
        be partitioned into fixed-size sequence slots before recording.

        If the buffer already has data or is in an incompatible state,
        this method automatically clears it before applying the configuration.

        Args:
            n_sequences: Number of recording sequence slots to allocate.
            frames_per_seq: Frames per sequence slot.
            pre_moi: Frames to keep before the MOI trigger.
            moi_source: "software", "external", or "acquisition_started"
                        (or MemoryBufferMOISource enum).
        """
        self._check_connected()
        moi = _resolve_enum(moi_source, reg.MemoryBufferMOISource)

        # Track configured sequence count for buffer_record()
        self._buffer_n_sequences = n_sequences
        self._buffer_next_sequence = 0

        # Try to enable buffer mode; if it fails, clean up stale state
        try:
            self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_MODE,
                                 reg.MemoryBufferMode.ON)
        except GVCPError:
            try:
                self._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)
            except GVCPError:
                pass
            time.sleep(0.3)
            try:
                self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_CLEAR_ALL, 1)
            except GVCPError:
                pass
            time.sleep(0.3)
            try:
                self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_MODE,
                                     reg.MemoryBufferMode.OFF)
            except GVCPError:
                pass
            time.sleep(0.3)
            self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_MODE,
                                 reg.MemoryBufferMode.ON)

        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_NUM_SEQUENCES, n_sequences)
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_SEQ_SIZE, frames_per_seq)
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_PRE_MOI_SIZE, pre_moi)
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_MOI_SOURCE, int(moi))

    def buffer_record(self, timeout: float = 30.0,
                      verbose: bool = True) -> int:
        """Record one sequence to the internal buffer.

        Arms the camera, fires software MOI, waits for recording to
        complete, and stops acquisition. Call this repeatedly to fill
        successive sequence slots (up to n_sequences configured).

        Args:
            timeout: Max seconds to wait for recording to finish.
            verbose: Print status messages (default True).

        Returns:
            Number of frames recorded in this sequence.

        Raises:
            RuntimeError: If all sequence slots are full.
            TimeoutError: If recording doesn't finish within timeout.
        """
        self._check_connected()
        n_seq = getattr(self, '_buffer_n_sequences', 1)
        seq_idx = getattr(self, '_buffer_next_sequence', 0)
        if seq_idx >= n_seq:
            raise RuntimeError(
                f"All {n_seq} sequence slots are full. "
                f"Call buffer_clear() or buffer_configure() first.")

        label = f" (seq {seq_idx + 1}/{n_seq})" if n_seq > 1 else ""

        if verbose:
            print(f"Arming{label}...", end=" ", flush=True)

        # Arm + start acquisition
        self._gvcp.write_reg(reg.REG_ACQUISITION_ARM, 1)
        self._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)

        # Wait for camera to enter RECORDING state before firing MOI
        time.sleep(0.5)

        if verbose:
            print("Recording...", end=" ", flush=True)

        # Fire software MOI
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_MOI_SOFTWARE, 1)

        # Wait for recording to complete
        self.buffer_wait(timeout=timeout)

        # Stop acquisition
        try:
            self._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)
        except GVCPError:
            pass

        recorded = self.buffer_recorded_frames(seq_idx)
        self._buffer_next_sequence = seq_idx + 1

        if verbose:
            print(f"Done ({recorded} frames)", flush=True)

        return recorded

    def buffer_arm(self) -> None:
        """Arm the camera and start acquisition for buffer recording.

        Use this for external trigger workflows where you need to arm
        the camera and wait for an external MOI signal. After the
        trigger fires, call buffer_wait() to block until recording
        completes.
        """
        self._check_connected()
        self._gvcp.write_reg(reg.REG_ACQUISITION_ARM, 1)
        self._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)

    def buffer_fire_moi(self) -> None:
        """Fire software MOI (Moment of Interest) trigger."""
        self._check_connected()
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_MOI_SOFTWARE, 1)

    def buffer_wait(self, timeout: float = 30.0,
                    poll_interval: float = 0.5) -> reg.MemoryBufferStatus:
        """Wait for buffer recording to complete.

        Polls buffer_status() until HOLDING or IDLE.

        Args:
            timeout: Max seconds to wait.
            poll_interval: Seconds between status polls.

        Returns:
            Final MemoryBufferStatus.

        Raises:
            TimeoutError: If not complete within timeout.
        """
        self._check_connected()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.buffer_status()
            if status in (reg.MemoryBufferStatus.HOLDING,
                          reg.MemoryBufferStatus.IDLE):
                return status
            time.sleep(poll_interval)
        raise TimeoutError(
            f"Buffer recording not complete after {timeout:.0f}s "
            f"(last status: {status.name})")

    def buffer_info(self) -> dict:
        """Summary of buffer state and recorded sequences.

        Returns:
            Dict with keys: status, n_sequences, recorded (list of
            frame counts per sequence), total_bytes, free_bytes.
        """
        self._check_connected()
        status = self.buffer_status()
        n_seq = getattr(self, '_buffer_n_sequences', 1)

        recorded = []
        for i in range(n_seq):
            try:
                self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_SEQ_SELECTOR, i)
                count = self._gvcp.read_reg(
                    reg.REG_MEMORY_BUFFER_SEQ_RECORDED_SIZE)
                recorded.append(count)
            except GVCPError:
                recorded.append(0)

        total_hi = self._gvcp.read_reg(reg.REG_MEMORY_BUFFER_TOTAL_SPACE_HIGH)
        total_lo = self._gvcp.read_reg(reg.REG_MEMORY_BUFFER_TOTAL_SPACE_LOW)
        free_hi = self._gvcp.read_reg(reg.REG_MEMORY_BUFFER_FREE_SPACE_HIGH)
        free_lo = self._gvcp.read_reg(reg.REG_MEMORY_BUFFER_FREE_SPACE_LOW)

        return {
            "status": status.name,
            "n_sequences": n_seq,
            "recorded": recorded,
            "total_bytes": (total_hi << 32) | total_lo,
            "free_bytes": (free_hi << 32) | free_lo,
        }

    def buffer_status(self) -> reg.MemoryBufferStatus:
        """Read memory buffer status.

        Returns:
            MemoryBufferStatus enum (DEACTIVATED, IDLE, HOLDING,
            RECORDING, UPDATING, TRANSMITTING, DEFRAGGING).
        """
        self._check_connected()
        try:
            self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_STATUS,
                                 reg.MemoryBufferStatus.REFRESH)
            time.sleep(0.1)
        except GVCPError:
            pass
        val = self._gvcp.read_reg(reg.REG_MEMORY_BUFFER_STATUS)
        return reg.MemoryBufferStatus(val)

    def buffer_recorded_frames(self, sequence: int = 0) -> int:
        """Get number of recorded frames in a sequence.

        Args:
            sequence: Sequence index (0-based).

        Returns:
            Number of frames recorded.

        Note:
            May raise GVCPError while buffer is actively recording.
        """
        self._check_connected()
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_SEQ_SELECTOR, sequence)
        return self._gvcp.read_reg(reg.REG_MEMORY_BUFFER_SEQ_RECORDED_SIZE)

    # Address for download bitrate limit (Float, Mbps, max 1000)
    _REG_DOWNLOAD_BITRATE_MAX = 0xEAD4

    def buffer_download(self, sequence: int = 0, start_frame: int = 0,
                        n_frames: int = 0, timeout: float = 0,
                        bitrate_mbps: float = 1000.0,
                        packet_size: int = 1500,
                        strip_header: bool = True,
                        verbose: bool = True
                        ) -> Optional[np.ndarray]:
        """Download frames from the internal memory buffer.

        Args:
            sequence: Sequence index to download.
            start_frame: Starting frame ID (0 = first recorded).
            n_frames: Number of frames (0 = all recorded).
            timeout: Total timeout in seconds (0 = auto-calculate).
            bitrate_mbps: Max download bitrate in Mbps (default 1000).
            packet_size: GVSP packet size in bytes (default 9000).
                         If you get data loss, try 3000 or 1500.
            strip_header: Remove Telops metadata rows (default True).
            verbose: Show progress bar/messages (default True).

        Returns:
            numpy array (N, H, W) or None on failure.
        """
        self._check_connected()

        # Select sequence and get info
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_SEQ_SELECTOR, sequence)

        if n_frames == 0:
            n_frames = self._gvcp.read_reg(
                reg.REG_MEMORY_BUFFER_SEQ_RECORDED_SIZE)

        if n_frames == 0:
            if verbose:
                print("No frames recorded in buffer")
            return None

        first_frame_id = self._gvcp.read_reg(
            reg.REG_MEMORY_BUFFER_SEQ_FIRST_FRAME_ID)
        if start_frame == 0:
            start_frame = first_frame_id

        if timeout <= 0:
            timeout = max(n_frames / 200.0 * 1.5 + 5.0, 10.0)

        # Set up progress bar
        pbar = None
        if verbose:
            from tqdm import tqdm
            pbar = tqdm(total=n_frames, unit="frame",
                        desc="Downloading")

        # Suppress GVSP "packets unrecoverable" warnings during download
        import logging
        gvsp_logger = logging.getLogger("pyTelops.gvsp")
        old_level = gvsp_logger.level
        gvsp_logger.setLevel(logging.CRITICAL)

        # Ensure acquisition is stopped before configuring download
        try:
            self._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)
        except GVCPError:
            pass
        time.sleep(0.2)

        # Configure download — mode MUST be set before other registers
        # (they are locked when mode == OFF)
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_DOWNLOAD_MODE,
                             reg.MemoryBufferDownloadMode.SEQUENCE)

        # Increase download bitrate (register unlocked now that mode != OFF)
        old_bitrate = None
        try:
            old_bitrate = self._gvcp.read_float(self._REG_DOWNLOAD_BITRATE_MAX)
            if bitrate_mbps != old_bitrate:
                self._gvcp.write_float(self._REG_DOWNLOAD_BITRATE_MAX,
                                       bitrate_mbps)
        except GVCPError:
            pass

        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_DOWNLOAD_FRAME_ID,
                             start_frame)
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_DOWNLOAD_FRAME_COUNT,
                             n_frames)

        # Start streaming
        self.start_stream()
        self._gvsp.resend_enabled = False

        # Override packet size for download (larger = faster)
        old_pkt_size = None
        if packet_size != 1500:
            pkt_reg = self._gvcp.read_reg(reg.REG_SC_PACKET_SIZE)
            old_pkt_size = pkt_reg & 0xFFFF
            flags = pkt_reg & 0xFFFF0000
            self._gvcp.write_reg(reg.REG_SC_PACKET_SIZE,
                                 flags | packet_size)
            self._gvsp._packet_data_size = packet_size - 8

        # Start download stream
        self._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)

        # Collect frames
        frames = []
        t_start = time.monotonic()
        deadline = time.monotonic() + timeout
        last_progress = t_start

        try:
            for i in range(n_frames):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                result = self._gvsp.get_frame(timeout=min(remaining, 10.0))
                if result is not None:
                    frames.append(result)
                    if pbar:
                        pbar.update(1)
                else:
                    result = self._gvsp.get_frame(timeout=2.0)
                    if result is not None:
                        frames.append(result)
                        if pbar:
                            pbar.update(1)
                    else:
                        break
        finally:
            try:
                self._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)
            except GVCPError:
                pass
            time.sleep(0.2)
            # Restore original bitrate before turning off download mode
            # (register locks again when mode == OFF)
            if old_bitrate is not None:
                try:
                    self._gvcp.write_float(self._REG_DOWNLOAD_BITRATE_MAX,
                                           old_bitrate)
                except GVCPError:
                    pass
            try:
                self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_DOWNLOAD_MODE,
                                     reg.MemoryBufferDownloadMode.OFF)
            except GVCPError:
                pass
            # Restore packet size to standard MTU
            if old_pkt_size is not None:
                try:
                    pkt_reg = self._gvcp.read_reg(reg.REG_SC_PACKET_SIZE)
                    flags = pkt_reg & 0xFFFF0000
                    self._gvcp.write_reg(reg.REG_SC_PACKET_SIZE,
                                         flags | old_pkt_size)
                    self._gvsp._packet_data_size = old_pkt_size - 8
                except GVCPError:
                    pass
            self._gvsp.resend_enabled = True
            self.stop_stream()
            gvsp_logger.setLevel(old_level)

        if pbar:
            pbar.close()

        elapsed = time.monotonic() - t_start
        if verbose and frames:
            fps = len(frames) / elapsed if elapsed > 0 else 0
            mbps = len(frames) * self._gvcp.read_reg(reg.REG_PAYLOAD_SIZE) \
                / elapsed / 1e6 if elapsed > 0 else 0
            print(f"Downloaded {len(frames)} frames in {elapsed:.1f}s "
                  f"({fps:.0f} fps, {mbps:.1f} MB/s)")

        if not frames:
            return None
        result = np.stack(frames)
        if strip_header:
            result = self._strip_headers(result)

        if verbose:
            self._download_diagnostics(result, n_frames)

        return result

    @staticmethod
    def _download_diagnostics(data: np.ndarray, expected: int) -> None:
        """Print data integrity summary after download."""
        n = data.shape[0]
        frame_means = data.mean(axis=tuple(range(1, data.ndim)))
        zero_frames = int(np.sum(frame_means == 0))
        row_sums = data.reshape(n, data.shape[1], -1).sum(axis=2)
        frames_with_zero_rows = int(np.sum(np.any(row_sums == 0, axis=1)))

        issues = []
        if n < expected:
            issues.append(f"{expected - n} frames missing")
        if zero_frames > 0:
            issues.append(f"{zero_frames} blank frames")
        if frames_with_zero_rows > 0:
            issues.append(f"{frames_with_zero_rows} frames with zero rows")

        if issues:
            print(f"Data check: WARNING — {', '.join(issues)}")
        else:
            print(f"Data check: OK — {n} frames, "
                  f"range [{data.min()}–{data.max()}], "
                  f"mean {data.mean():.0f}")

    def buffer_clear(self) -> None:
        """Clear all sequences from the memory buffer."""
        self._check_connected()
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_CLEAR_ALL, 1)

    # ==========================================================
    # GUI
    # ==========================================================

    def live_view(self, colormap: str = "inferno", scale: int = 2) -> None:
        """Open a live thermal image viewer window.

        Requires the 'gui' extra: ``pip install pyTelops[gui]``

        Args:
            colormap: Matplotlib colormap name.
            scale: Display upscale factor (2 = double size).
        """
        from .gui import LiveView
        viewer = LiveView(self, colormap=colormap, scale=scale)
        viewer.run()

    # ==========================================================
    # Low-level Register Access
    # ==========================================================

    def read_register(self, addr: int) -> int:
        """Read a raw 32-bit register value."""
        self._check_connected()
        return self._gvcp.read_reg(addr)

    def write_register(self, addr: int, value: int) -> None:
        """Write a raw 32-bit register value."""
        self._check_connected()
        self._gvcp.write_reg(addr, value)

    def read_float_register(self, addr: int) -> float:
        """Read a register as IEEE 754 float."""
        self._check_connected()
        return self._gvcp.read_float(addr)

    def write_float_register(self, addr: int, value: float) -> None:
        """Write a register as IEEE 754 float."""
        self._check_connected()
        self._gvcp.write_float(addr, value)
