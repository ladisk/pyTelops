"""
High-level Telops camera driver.

Provides a clean, Pythonic interface to Telops FAST-series thermal cameras
over GigE Vision. Handles discovery, streaming, buffer operations, and
camera configuration.

Usage:
    from pyTelops import Camera

    with Camera() as cam:
        cam.integration_time = 50.0
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
    reg.ImageCorrectionMode: {
        "black_body": reg.ImageCorrectionMode.BLACK_BODY,
        "blackbody": reg.ImageCorrectionMode.BLACK_BODY,
        "icu": reg.ImageCorrectionMode.ICU,
    },
    reg.TestImageSelector: {
        "off": reg.TestImageSelector.OFF,
        "static": reg.TestImageSelector.STATIC_SHADE,
        "dynamic": reg.TestImageSelector.DYNAMIC_SHADE,
        "constant": reg.TestImageSelector.CONSTANT_VALUE,
    },
    reg.FrameRateMode: {
        "fixed_locked": reg.FrameRateMode.FIXED_LOCKED,
        "locked": reg.FrameRateMode.FIXED_LOCKED,
        "fixed": reg.FrameRateMode.FIXED,
        "maximum": reg.FrameRateMode.MAXIMUM,
        "max": reg.FrameRateMode.MAXIMUM,
        "burst": reg.FrameRateMode.BURST,
    },
    reg.TemperatureLocation: {
        "sensor": reg.TemperatureLocation.SENSOR,
        "mainboard": reg.TemperatureLocation.MAINBOARD,
        "compressor": reg.TemperatureLocation.COMPRESSOR,
        "cold_finger": reg.TemperatureLocation.COLD_FINGER,
        "processing_fpga": reg.TemperatureLocation.PROCESSING_FPGA,
        "output_fpga": reg.TemperatureLocation.OUTPUT_FPGA,
        "storage_fpga": reg.TemperatureLocation.STORAGE_FPGA,
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
            cam.integration_time = 100.0
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

        # Auto-wait if camera is not ready (cooling down, initializing, etc.)
        try:
            if self._gvcp.read_reg(reg.REG_DEVICE_NOT_READY):
                self.wait_until_ready()
        except GVCPError:
            pass

        # Apply sensible defaults (after camera is ready so writes succeed)
        try:
            self._gvcp.write_reg(reg.REG_BAD_PIXEL_REPLACEMENT, 1)
        except GVCPError:
            pass
        try:
            self._gvcp.write_reg(reg.REG_FRAME_RATE_MODE,
                                 reg.FrameRateMode.FIXED)
        except GVCPError:
            pass
        try:
            self._gvcp.write_float(reg.REG_FRAME_RATE_MAX_FG, 1e9)
        except GVCPError:
            pass
        try:
            self._gvcp.write_reg(reg.REG_TEST_IMAGE_SELECTOR,
                                 reg.TestImageSelector.OFF)
        except GVCPError:
            pass

    def wait_until_ready(self, timeout: float = 120.0,
                         verbose: bool = True) -> None:
        """Wait for camera to be ready (cooled down, initialized).

        Automatically called by grab(), acquire(), and buffer_record()
        if the camera is not ready. Shows a single updating status line.

        Args:
            timeout: Max seconds to wait.
            verbose: Print status updates.

        Raises:
            TimeoutError: If camera not ready within timeout.
        """
        self._check_connected()

        _TDC_REASONS = {
            reg.TDC_WAITING_FOR_COOLER: "Cooling down",
            reg.TDC_WAITING_FOR_SENSOR: "Sensor initializing",
            reg.TDC_WAITING_FOR_INIT: "Device initializing",
            reg.TDC_WAITING_FOR_ICU: "Calibration unit warming up",
            reg.TDC_WAITING_FOR_CAL_INIT: "Loading calibration",
            reg.TDC_WAITING_FOR_CAL_DATA: "Loading calibration data",
            reg.TDC_WAITING_FOR_IMAGE_CORRECTION: "Image correction",
            reg.TDC_WAITING_FOR_OUTPUT_FPGA: "Output FPGA initializing",
            reg.TDC_WAITING_FOR_POWER_ON: "Powering on",
            reg.TDC_WAITING_FOR_FLASH_SETTINGS: "Loading saved settings",
            reg.TDC_WAITING_FOR_VALID_PARAMS: "Invalid parameters",
        }

        deadline = time.monotonic() + timeout
        printed = False

        while time.monotonic() < deadline:
            not_ready = self._gvcp.read_reg(reg.REG_DEVICE_NOT_READY)
            if not not_ready:
                if verbose and printed:
                    elapsed = timeout - (deadline - time.monotonic())
                    print(f"\rCamera ready. ({elapsed:.0f}s)          ",
                          flush=True)
                return

            # Build status message
            tdc = self._gvcp.read_reg(reg.REG_TDC_STATUS)
            tdc &= ~reg.TDC_ACQUISITION_STARTED

            reasons = [desc for flag, desc in _TDC_REASONS.items()
                       if tdc & flag]
            msg = ", ".join(reasons) if reasons else "Not ready"

            if tdc & reg.TDC_WAITING_FOR_COOLER:
                try:
                    temp = self.sensor_temperature("sensor")
                    msg += f" ({temp:.1f} C)"
                except Exception:
                    pass

            elapsed = timeout - (deadline - time.monotonic())
            if verbose:
                print(f"\rWaiting: {msg} [{elapsed:.0f}s]          ",
                      end="", flush=True)
                printed = True

            time.sleep(2.0)

        raise TimeoutError(f"Camera not ready after {timeout:.0f}s")

    @property
    def tdc_status(self) -> int:
        """Raw TDC Status bitmask (see TDC_* constants in registers)."""
        self._check_connected()
        return self._gvcp.read_reg(reg.REG_TDC_STATUS)

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

    def _check_ready(self):
        """If camera is not ready, auto-wait with a status line."""
        self._check_connected()
        try:
            not_ready = self._gvcp.read_reg(reg.REG_DEVICE_NOT_READY)
            if not_ready:
                self.wait_until_ready()
        except GVCPError:
            pass

    def _check_fps_clamped(self, fps_before: float):
        """Warn if a settings change caused the frame rate to be clamped."""
        fps_after = self._gvcp.read_float(reg.REG_ACQUISITION_FRAME_RATE)
        if fps_after < fps_before - 0.5:
            max_hz = self._gvcp.read_float(reg.REG_FRAME_RATE_MAX)
            import warnings
            warnings.warn(
                f"Frame rate was reduced from {fps_before:.0f} to "
                f"{fps_after:.0f} Hz (max for current settings: "
                f"{max_hz:.0f} Hz).",
                UserWarning, stacklevel=3)

    @property
    def integration_time(self) -> float:
        """Integration time in microseconds."""
        self._check_connected()
        return self._gvcp.read_float(reg.REG_EXPOSURE_TIME)

    @integration_time.setter
    def integration_time(self, us: float):
        self._check_connected()
        # Disable AEC if active (it locks ExposureTime register)
        aec = self._gvcp.read_reg(reg.REG_EXPOSURE_AUTO)
        if aec != reg.ExposureAuto.OFF:
            self._gvcp.write_reg(reg.REG_EXPOSURE_AUTO, reg.ExposureAuto.OFF)
        fps_before = self._gvcp.read_float(reg.REG_ACQUISITION_FRAME_RATE)
        self._gvcp.write_float(reg.REG_EXPOSURE_TIME, us)
        self._check_fps_clamped(fps_before)

    # Backward-compatible alias
    exposure = integration_time

    @property
    def integration_time_auto(self) -> reg.ExposureAuto:
        """Auto integration time control mode (OFF, ONCE, CONTINUOUS)."""
        self._check_connected()
        return reg.ExposureAuto(self._gvcp.read_reg(reg.REG_EXPOSURE_AUTO))

    @integration_time_auto.setter
    def integration_time_auto(self, mode):
        self._check_connected()
        self._gvcp.write_reg(reg.REG_EXPOSURE_AUTO,
                             int(_resolve_enum(mode, reg.ExposureAuto)))

    # Backward-compatible alias
    exposure_auto = integration_time_auto

    @property
    def frame_rate(self) -> float:
        """Acquisition frame rate in Hz."""
        self._check_connected()
        return self._gvcp.read_float(reg.REG_ACQUISITION_FRAME_RATE)

    @property
    def frame_rate_max(self) -> float:
        """Maximum frame rate in Hz for current resolution and integration time."""
        self._check_connected()
        return self._gvcp.read_float(reg.REG_FRAME_RATE_MAX)

    @frame_rate.setter
    def frame_rate(self, hz: float):
        self._check_connected()
        max_hz = self._gvcp.read_float(reg.REG_FRAME_RATE_MAX)
        self._gvcp.write_float(reg.REG_ACQUISITION_FRAME_RATE, hz)
        if hz > max_hz:
            actual = self._gvcp.read_float(reg.REG_ACQUISITION_FRAME_RATE)
            import warnings
            warnings.warn(
                f"Requested {hz:.0f} Hz exceeds max {max_hz:.0f} Hz "
                f"(at current resolution/integration time). "
                f"Camera clamped to {actual:.0f} Hz.",
                UserWarning, stacklevel=2)

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
        fps_before = self._gvcp.read_float(reg.REG_ACQUISITION_FRAME_RATE)
        self._gvcp.write_reg(reg.REG_WIDTH, wh[0])
        self._gvcp.write_reg(reg.REG_HEIGHT, wh[1])
        self._check_fps_clamped(fps_before)

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
            "integration_time_us": self._gvcp.read_float(reg.REG_EXPOSURE_TIME),
            "integration_time_auto": reg.ExposureAuto(
                self._gvcp.read_reg(reg.REG_EXPOSURE_AUTO)).name,
            "frame_rate_hz": self._gvcp.read_float(
                reg.REG_ACQUISITION_FRAME_RATE),
            "frame_rate_max_hz": self._gvcp.read_float(
                reg.REG_FRAME_RATE_MAX),
            "calibration": reg.CalibrationMode(
                self._gvcp.read_reg(reg.REG_CALIBRATION_MODE)).name,
            "trigger_mode": reg.TriggerMode(
                self._gvcp.read_reg(reg.REG_TRIGGER_MODE)).name,
            "power_state": reg.DevicePowerState(
                self._gvcp.read_reg(reg.REG_DEVICE_POWER_STATE)).name,
            "temperature_c": self._gvcp.read_float(reg.REG_DEVICE_TEMPERATURE),
            "buffer_mode": reg.MemoryBufferMode(
                self._gvcp.read_reg(reg.REG_MEMORY_BUFFER_MODE)).name,
            "bad_pixel_replacement": bool(self._gvcp.read_reg(reg.REG_BAD_PIXEL_REPLACEMENT)),
            "reverse_x": bool(self._gvcp.read_reg(reg.REG_REVERSE_X)),
            "reverse_y": bool(self._gvcp.read_reg(reg.REG_REVERSE_Y)),
            "test_image": reg.TestImageSelector(self._gvcp.read_reg(reg.REG_TEST_IMAGE_SELECTOR)).name,
            "frame_rate_mode": reg.FrameRateMode(self._gvcp.read_reg(reg.REG_FRAME_RATE_MODE)).name,
            "roi_offset": (self._gvcp.read_reg(reg.REG_OFFSET_X), self._gvcp.read_reg(reg.REG_OFFSET_Y)),
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

    @property
    def bad_pixel_replacement(self) -> bool:
        """Bad pixel auto-replacement. ON by default (replaces with neighbor value)."""
        self._check_connected()
        return bool(self._gvcp.read_reg(reg.REG_BAD_PIXEL_REPLACEMENT))

    @bad_pixel_replacement.setter
    def bad_pixel_replacement(self, enabled: bool):
        self._check_connected()
        self._gvcp.write_reg(reg.REG_BAD_PIXEL_REPLACEMENT, int(enabled))

    @property
    def reverse_x(self) -> bool:
        """Horizontal image flip."""
        self._check_connected()
        return bool(self._gvcp.read_reg(reg.REG_REVERSE_X))

    @reverse_x.setter
    def reverse_x(self, enabled: bool):
        self._check_connected()
        self._gvcp.write_reg(reg.REG_REVERSE_X, int(enabled))

    @property
    def reverse_y(self) -> bool:
        """Vertical image flip."""
        self._check_connected()
        return bool(self._gvcp.read_reg(reg.REG_REVERSE_Y))

    @reverse_y.setter
    def reverse_y(self, enabled: bool):
        self._check_connected()
        self._gvcp.write_reg(reg.REG_REVERSE_Y, int(enabled))

    @property
    def test_image(self):
        """Test image source ("off" for normal operation)."""
        self._check_connected()
        return reg.TestImageSelector(self._gvcp.read_reg(reg.REG_TEST_IMAGE_SELECTOR))

    @test_image.setter
    def test_image(self, mode):
        self._check_connected()
        self._gvcp.write_reg(reg.REG_TEST_IMAGE_SELECTOR,
                             int(_resolve_enum(mode, reg.TestImageSelector)))

    @property
    def roi_offset(self) -> tuple[int, int]:
        """ROI offset as (x, y) pixels."""
        self._check_connected()
        return (self._gvcp.read_reg(reg.REG_OFFSET_X),
                self._gvcp.read_reg(reg.REG_OFFSET_Y))

    @roi_offset.setter
    def roi_offset(self, xy: tuple[int, int]):
        self._check_connected()
        self._gvcp.write_reg(reg.REG_OFFSET_X, xy[0])
        self._gvcp.write_reg(reg.REG_OFFSET_Y, xy[1])

    @property
    def frame_rate_mode(self):
        """Frame rate mode (FIXED, FIXED_LOCKED, MAXIMUM, BURST)."""
        self._check_connected()
        return reg.FrameRateMode(self._gvcp.read_reg(reg.REG_FRAME_RATE_MODE))

    @frame_rate_mode.setter
    def frame_rate_mode(self, mode):
        self._check_connected()
        self._gvcp.write_reg(reg.REG_FRAME_RATE_MODE,
                             int(_resolve_enum(mode, reg.FrameRateMode)))

    @property
    def trigger_frame_count(self) -> int:
        """Frames per trigger event (for burst capture)."""
        self._check_connected()
        return self._gvcp.read_reg(reg.REG_TRIGGER_FRAME_COUNT)

    @trigger_frame_count.setter
    def trigger_frame_count(self, count: int):
        self._check_connected()
        self._gvcp.write_reg(reg.REG_TRIGGER_FRAME_COUNT, count)

    # ==========================================================
    # Streaming
    # ==========================================================

    def start_stream(self) -> None:
        """Configure stream channel and start GVSP receiver."""
        self._check_connected()
        if self._streaming:
            return

        # Clamp packet size to standard MTU, preserving flag bits
        target_pkt_size = 1500
        pkt_reg = self._gvcp.read_reg(reg.REG_SC_PACKET_SIZE)
        current_size = pkt_reg & reg.SC_PACKET_SIZE_MASK
        if current_size != target_pkt_size:
            # Preserve upper flags and lower flag bits (DoNotFragment etc.)
            non_size_bits = pkt_reg & ~reg.SC_PACKET_SIZE_MASK
            self._gvcp.write_reg(reg.REG_SC_PACKET_SIZE,
                                 non_size_bits | target_pkt_size)

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

    # RT mode outputs centi-Kelvin as uint16 (e.g., 29315 = 293.15 K = 20 C)
    RT_SCALE = 100.0

    def _strip_headers(self, arr: np.ndarray) -> np.ndarray:
        """Strip Telops header rows from a frame or batch of frames."""
        if self.HEADER_ROWS == 0:
            return arr
        if arr.ndim == 2:
            return arr[self.HEADER_ROWS:, :]
        elif arr.ndim == 3:
            return arr[:, self.HEADER_ROWS:, :]
        return arr

    def _to_kelvin(self, arr: np.ndarray) -> np.ndarray:
        """Convert RT mode centi-Kelvin uint16 to Kelvin float32."""
        return arr.astype(np.float32) / self.RT_SCALE

    def _is_rt_mode(self) -> bool:
        """Check if camera is in radiometric temperature mode."""
        try:
            return self._gvcp.read_reg(reg.REG_CALIBRATION_MODE) == reg.CalibrationMode.RT
        except GVCPError:
            return False

    def grab(self, timeout: float = 5.0,
             strip_header: bool = True,
             convert: bool = True) -> Optional[np.ndarray]:
        """Grab a single frame.

        Starts streaming if not already active, grabs one frame.

        Args:
            timeout: Seconds to wait for a frame.
            strip_header: Remove Telops metadata rows (default True).
            convert: Convert to Kelvin in RT mode (default True).
                     Set False for raw uint16 values.

        Returns:
            2D numpy array (H, W). Float32 Kelvin in RT mode,
            uint16 raw counts otherwise. None on timeout.
        """
        self._check_ready()
        was_streaming = self._streaming
        if not self._streaming:
            self.start_stream()
            self._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)

        try:
            frame = self._gvsp.get_frame(timeout=timeout)
        finally:
            if not was_streaming:
                self.stop_stream()

        if frame is not None:
            if strip_header:
                frame = self._strip_headers(frame)
            if convert and self._is_rt_mode():
                frame = self._to_kelvin(frame)
        return frame

    def acquire(self, n_frames: int, timeout: float = 30.0,
                strip_header: bool = True,
                convert: bool = True) -> Optional[np.ndarray]:
        """Acquire multiple frames via live streaming.

        Args:
            n_frames: Number of frames to capture.
            timeout: Total timeout in seconds.
            strip_header: Remove Telops metadata rows (default True).
            convert: Convert to Kelvin in RT mode (default True).

        Returns:
            3D numpy array (N, H, W) or None if no frames captured.
            Float32 Kelvin in RT mode, uint16 raw counts otherwise.
        """
        self._check_ready()
        was_streaming = self._streaming
        if not self._streaming:
            self.start_stream()
            self._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)

        frames = []
        try:
            deadline = time.monotonic() + timeout
            for i in range(n_frames):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                result = self._gvsp.get_frame(timeout=remaining)
                if result is not None:
                    frames.append(result)
        finally:
            if not was_streaming:
                self.stop_stream()

        if not frames:
            return None
        result = np.stack(frames)
        if strip_header:
            result = self._strip_headers(result)
        if convert and self._is_rt_mode():
            result = self._to_kelvin(result)
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
    # Image Processing
    # ==========================================================

    def nuc(self, mode="black_body", blackbody_temp=None, timeout=60.0):
        """Perform Non-Uniformity Correction (NUC).

        Blocks until complete. Locks many camera registers while running.

        Args:
            mode: "black_body" or "icu" (or ImageCorrectionMode enum).
            blackbody_temp: Temperature in Celsius (for black body mode).
            timeout: Max seconds to wait.
        """
        self._check_connected()
        m = _resolve_enum(mode, reg.ImageCorrectionMode)
        self._gvcp.write_reg(reg.REG_IMAGE_CORRECTION_MODE, int(m))
        if blackbody_temp is not None:
            self._gvcp.write_float(reg.REG_EXTERNAL_BLACKBODY_TEMP, blackbody_temp)
        self._gvcp.write_reg(reg.REG_IMAGE_CORRECTION, 1)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._gvcp.read_reg(reg.REG_DEVICE_NOT_READY):
                return
            time.sleep(1.0)
        raise TimeoutError(f"NUC did not complete within {timeout:.0f}s")

    # ==========================================================
    # Diagnostics
    # ==========================================================

    def sensor_temperature(self, location="sensor") -> float:
        """Read temperature at a specific sensor location.

        Args:
            location: "sensor", "compressor", "cold_finger", "processing_fpga",
                      etc. (or TemperatureLocation enum).
        """
        self._check_connected()
        loc = _resolve_enum(location, reg.TemperatureLocation)
        self._gvcp.write_reg(reg.REG_DEVICE_TEMPERATURE_SELECTOR, int(loc))
        return self._gvcp.read_float(reg.REG_DEVICE_TEMPERATURE_READOUT)

    def diagnostics(self) -> dict:
        """Read all diagnostic sensors (temperatures, voltages, currents, uptime).

        Involves ~40 register reads, may take a few hundred milliseconds.
        Sensors not available on this model return None.
        """
        self._check_connected()

        temps = {}
        for loc in reg.TemperatureLocation:
            try:
                self._gvcp.write_reg(reg.REG_DEVICE_TEMPERATURE_SELECTOR, int(loc))
                temps[loc.name.lower()] = self._gvcp.read_float(reg.REG_DEVICE_TEMPERATURE_READOUT)
            except GVCPError:
                temps[loc.name.lower()] = None

        voltages = {}
        for src in reg.VoltageLocation:
            try:
                self._gvcp.write_reg(reg.REG_DEVICE_VOLTAGE_SELECTOR, int(src))
                voltages[src.name.lower()] = self._gvcp.read_float(reg.REG_DEVICE_VOLTAGE_READOUT)
            except GVCPError:
                voltages[src.name.lower()] = None

        currents = {}
        for src in reg.CurrentLocation:
            try:
                self._gvcp.write_reg(reg.REG_DEVICE_CURRENT_SELECTOR, int(src))
                currents[src.name.lower()] = self._gvcp.read_float(reg.REG_DEVICE_CURRENT_READOUT)
            except GVCPError:
                currents[src.name.lower()] = None

        return {
            "temperatures": temps,
            "voltages": voltages,
            "currents": currents,
            "device_running_s": self._gvcp.read_reg(reg.REG_DEVICE_RUNNING_TIME),
            "cooler_running_s": self._gvcp.read_reg(reg.REG_DEVICE_COOLER_RUNNING_TIME),
            "power_on_cycles": self._gvcp.read_reg(reg.REG_DEVICE_POWER_ON_CYCLES),
            "cooler_power_on_cycles": self._gvcp.read_reg(reg.REG_DEVICE_COOLER_POWER_ON_CYCLES),
        }

    # ==========================================================
    # Device Management
    # ==========================================================

    def save_config(self) -> None:
        """Save current configuration to camera non-volatile memory."""
        self._check_connected()
        self._gvcp.write_reg(reg.REG_SAVE_CONFIGURATION, 1)

    def sync_time(self) -> None:
        """Synchronize camera clock to host system time (UTC)."""
        import datetime
        self._check_connected()
        now = datetime.datetime.now(datetime.timezone.utc)
        self._gvcp.write_reg(reg.REG_POSIX_TIME, int(now.timestamp()))

    @property
    def posix_time(self):
        """Camera time as Python datetime (UTC)."""
        import datetime
        self._check_connected()
        seconds = self._gvcp.read_reg(reg.REG_POSIX_TIME)
        sub_100ns = self._gvcp.read_reg(reg.REG_SUB_SECOND_TIME)
        microseconds = sub_100ns // 10  # 100ns ticks -> microseconds
        return datetime.datetime.fromtimestamp(
            seconds, tz=datetime.timezone.utc
        ).replace(microsecond=microseconds)

    @posix_time.setter
    def posix_time(self, dt):
        """Set camera time from a datetime object."""
        self._check_connected()
        if hasattr(dt, 'timestamp'):
            self._gvcp.write_reg(reg.REG_POSIX_TIME, int(dt.timestamp()))
        else:
            self._gvcp.write_reg(reg.REG_POSIX_TIME, int(dt))

    @property
    def gev_timestamp_ns(self) -> int:
        """GigE Vision timestamp in nanoseconds (read-only)."""
        self._check_connected()
        # Latch timestamp
        self._gvcp.write_reg(reg.REG_GEV_TIMESTAMP_CONTROL, 2)

        tick_hi = self._gvcp.read_reg(reg.REG_GEV_TIMESTAMP_VALUE_HIGH)
        tick_lo = self._gvcp.read_reg(reg.REG_GEV_TIMESTAMP_VALUE_LOW)
        ticks = (tick_hi << 32) | tick_lo

        freq_hi = self._gvcp.read_reg(reg.REG_GEV_TIMESTAMP_TICK_FREQ_HIGH)
        freq_lo = self._gvcp.read_reg(reg.REG_GEV_TIMESTAMP_TICK_FREQ_LOW)
        freq = (freq_hi << 32) | freq_lo

        if freq == 0:
            return ticks
        return int(ticks * 1_000_000_000 / freq)

    # ==========================================================
    # Memory Buffer (16GB onboard)
    # ==========================================================

    def buffer_configure(self, n_sequences: int = 1,
                         duration: Optional[float] = None,
                         frames_per_seq: Optional[int] = None,
                         pre_moi: int = 0,
                         moi_source="software") -> None:
        """Configure the internal memory buffer for recording.

        The camera has a 16GB ring buffer that records at full sensor speed
        (up to 3100 fps at full frame), independent of the Ethernet link.
        The buffer must be partitioned into fixed-size sequence slots
        before recording.

        Specify either ``duration`` (seconds, uses current frame_rate to
        calculate frame count) or ``frames_per_seq`` (exact frame count).

        If the buffer already has data or is in an incompatible state,
        this method automatically clears it before applying the configuration.

        Args:
            n_sequences: Number of recording sequence slots to allocate.
            duration: Recording duration per sequence in seconds.
                      Calculates frames_per_seq from current frame_rate.
            frames_per_seq: Frames per sequence slot (alternative to duration).
            pre_moi: Frames to keep before the MOI trigger.
            moi_source: "software", "external", or "acquisition_started"
                        (or MemoryBufferMOISource enum).
        """
        self._check_connected()
        moi = _resolve_enum(moi_source, reg.MemoryBufferMOISource)

        # Resolve frame count from duration or frames_per_seq
        if duration is not None and frames_per_seq is not None:
            raise ValueError("Specify either duration or frames_per_seq, not both.")
        if duration is not None:
            fps = self._gvcp.read_float(reg.REG_ACQUISITION_FRAME_RATE)
            frames_per_seq = int(duration * fps)
            if frames_per_seq <= 0:
                raise ValueError(
                    f"duration={duration}s at {fps:.0f} fps = {frames_per_seq} "
                    f"frames. Set frame_rate first.")
        elif frames_per_seq is None:
            frames_per_seq = 100

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

    def buffer_record(self, verbose: bool = True) -> int:
        """Record all configured sequences to the internal buffer.

        Supports both single-sequence and multi-sequence recordings.
        For each sequence, arms (first only), fires a software MOI, and
        waits for the sequence to complete by polling the
        ``MemoryBufferSequenceCount`` register (0xE914).

        Single-sequence example::

            cam.buffer_configure(n_sequences=1, frames_per_seq=100)
            n = cam.buffer_record()  # -> 100

        Multi-sequence example::

            cam.buffer_configure(n_sequences=3, frames_per_seq=50)
            n = cam.buffer_record()  # records all 3, returns total

        For external-trigger workflows where the MOI comes from an
        outside signal, use the manual flow instead::

            cam.buffer_configure(n_sequences=3, moi_source="external")
            cam.buffer_arm()
            # ... external trigger fires 3 times ...
            cam.buffer_wait()       # waits for HOLDING/IDLE

        Args:
            verbose: Print status messages (default True).

        Returns:
            Total number of frames recorded across all sequences.

        Raises:
            TimeoutError: If a sequence doesn't finish within the
                safety timeout.
        """
        self._check_ready()
        n_seq = getattr(self, '_buffer_n_sequences', 1)

        # Auto-calculate per-sequence timeout from frame count and frame rate
        fps = self._gvcp.read_float(reg.REG_ACQUISITION_FRAME_RATE)
        seq_size = self._gvcp.read_reg(reg.REG_MEMORY_BUFFER_SEQ_SIZE)
        if fps > 0:
            recording_time = seq_size / fps
            timeout = max(recording_time * 2 + 30, 45.0)
        else:
            timeout = 60.0

        total_recorded = 0

        for seq_idx in range(n_seq):
            if seq_idx == 0:
                # First sequence: arm + start + settle + fire MOI
                if verbose:
                    print(f"Arming (seq {seq_idx + 1}/{n_seq})...",
                          end=" ", flush=True)

                self._gvcp.write_reg(reg.REG_ACQUISITION_ARM, 1)
                self._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)
                time.sleep(0.5)
            else:
                # Subsequent sequences: camera stays armed, just fire MOI
                if verbose:
                    print(f"Firing (seq {seq_idx + 1}/{n_seq})...",
                          end=" ", flush=True)

            if verbose:
                print("Recording...", end=" ", flush=True)

            self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_MOI_SOFTWARE, 1)

            # Wait for this sequence to complete
            try:
                self._buffer_wait_sequence(seq_idx + 1, timeout=timeout)
            except TimeoutError:
                # On timeout of the last sequence, stop acquisition
                try:
                    self._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)
                except GVCPError:
                    pass
                if verbose:
                    print("TIMEOUT", flush=True)
                raise

            if verbose:
                print(f"Done ({seq_size} frames)", flush=True)
            total_recorded += seq_size

        # Stop acquisition after all sequences complete
        try:
            self._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)
        except GVCPError:
            pass
        time.sleep(0.3)

        # Now read actual per-sequence counts (registers unlocked after stop)
        total_recorded = 0
        for i in range(n_seq):
            total_recorded += self.buffer_recorded_frames(i)

        return total_recorded

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
        status = self.buffer_status()
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

    def _buffer_wait_sequence(self, target_count: int,
                              timeout: float = 30.0,
                              poll_interval: float = 0.5) -> None:
        """Wait for the sequence counter to reach *target_count*.

        Polls ``MemoryBufferSequenceCount`` (0xE914) which increments
        each time a sequence finishes recording.  This allows
        per-sequence completion detection without waiting for the
        overall buffer status to leave RECORDING (which only happens
        after the *last* configured sequence).

        Args:
            target_count: Expected value of the sequence counter
                (1 after first sequence completes, 2 after second, ...).
            timeout: Max seconds to wait.
            poll_interval: Seconds between polls.

        Raises:
            TimeoutError: If *target_count* is not reached in time.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            count = self._gvcp.read_reg(reg.REG_MEMORY_BUFFER_SEQ_COUNT)
            if count >= target_count:
                return
            time.sleep(poll_interval)
        raise TimeoutError(
            f"Sequence count did not reach {target_count} within "
            f"{timeout:.0f}s (current: "
            f"{self._gvcp.read_reg(reg.REG_MEMORY_BUFFER_SEQ_COUNT)})")

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

    def buffer_download(self, sequence: int = 0,
                        start_frame: Optional[int] = None,
                        n_frames: int = 0, timeout: float = 0,
                        bitrate_mbps: float = 1000.0,
                        packet_size: int = 1500,
                        strip_header: bool = True,
                        convert: bool = True,
                        verbose: bool = True
                        ) -> Optional[np.ndarray]:
        """Download frames from the internal memory buffer.

        Args:
            sequence: Sequence index to download.
            start_frame: Starting frame ID (None = first recorded).
            n_frames: Number of frames (0 = all recorded).
            timeout: Total timeout in seconds (0 = auto-calculate).
            bitrate_mbps: Max download bitrate in Mbps (default 1000).
            packet_size: GVSP packet size in bytes (default 1500).
                         Try 9000 for faster downloads if your network
                         supports it.
            strip_header: Remove Telops metadata rows (default True).
            convert: Convert to Kelvin in RT mode (default True).
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
        if start_frame is None:
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
            old_bitrate = self._gvcp.read_float(reg.REG_DOWNLOAD_BITRATE_MAX)
            if bitrate_mbps != old_bitrate:
                self._gvcp.write_float(reg.REG_DOWNLOAD_BITRATE_MAX,
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

        # Override packet size for download (larger = faster).
        # When using packet_size > 1500 (e.g. 9000), UDP packets will be
        # IP-fragmented by the camera.  If GevSCPSDoNotFragment (bit 1)
        # is set, the camera/NIC drops oversized packets instead of
        # fragmenting them, causing complete data loss.  We clear the
        # flag when using large packets and restore the original register
        # value afterwards.
        old_pkt_reg = None
        if packet_size != 1500:
            old_pkt_reg = self._gvcp.read_reg(reg.REG_SC_PACKET_SIZE)
            upper_flags = old_pkt_reg & 0xFFFF0000
            new_pkt_reg = upper_flags | (packet_size & reg.SC_PACKET_SIZE_MASK)
            if packet_size > 1500:
                # Allow IP fragmentation for large packets
                new_pkt_reg &= ~reg.SC_SCPS_DO_NOT_FRAGMENT
            self._gvcp.write_reg(reg.REG_SC_PACKET_SIZE, new_pkt_reg)
            self._gvsp._packet_data_size = packet_size - 8

        # Start download stream
        self._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)

        # Collect frames
        frames = []
        t_start = time.monotonic()
        deadline = time.monotonic() + timeout

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
            if pbar:
                pbar.close()
            try:
                self._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)
            except GVCPError:
                pass
            time.sleep(0.2)
            if old_bitrate is not None:
                try:
                    self._gvcp.write_float(reg.REG_DOWNLOAD_BITRATE_MAX,
                                           old_bitrate)
                except GVCPError:
                    pass
            try:
                self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_DOWNLOAD_MODE,
                                     reg.MemoryBufferDownloadMode.OFF)
            except GVCPError:
                pass
            if old_pkt_reg is not None:
                try:
                    # Restore original register value (size + flags incl.
                    # DoNotFragment) exactly as it was before download.
                    self._gvcp.write_reg(reg.REG_SC_PACKET_SIZE, old_pkt_reg)
                    self._gvsp._packet_data_size = (old_pkt_reg & reg.SC_PACKET_SIZE_MASK) - 8
                except GVCPError:
                    pass
            self._gvsp.resend_enabled = True
            self.stop_stream()
            gvsp_logger.setLevel(old_level)

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
        if convert and self._is_rt_mode():
            result = self._to_kelvin(result)

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
