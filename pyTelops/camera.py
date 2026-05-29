"""
High-level Telops camera driver.

This module exposes the :class:`Camera` class, which provides a Pythonic
interface to Telops FAST-series thermal cameras over GigE Vision 1.2. Control
messages are exchanged over GVCP (UDP register read/write) via
:class:`pyGigEVision.GVCPClient`; image data arrives over GVSP via
:class:`pyGigEVision.GVSPReceiver`. Register address constants and enum
definitions live in :mod:`pyTelops.registers`.

Usable-pixels convention
------------------------
Telops cameras embed two metadata rows at the top of every frame. The driver
strips them transparently so that all user-facing values (resolution,
``grab()``, ``acquire()``, buffer downloads) are in *usable pixels*. The
constant :attr:`Camera.HEADER_ROWS` (= 2) records this offset; when writing
height to the camera register the driver adds it back automatically.

Calibration and physical units
-------------------------------
When :attr:`Camera.calibration_mode` is ``"RT"``, frames are delivered in
degrees Celsius. The per-frame header contains a DataExp and DataOffset that
encode the Kelvin conversion; the driver applies ``pixel * 2**DataExp +
DataOffset - 273.15`` automatically. In ``"NUC"`` or ``"RAW"`` mode the
driver returns the raw 16-bit integer values unchanged.

Lifecycle
---------
The standard pattern is a context manager::

    from pyTelops import Camera

    with Camera() as cam:
        cam.integration_time = 50.0
        frame = cam.grab()

For manual control::

    cam = Camera(ip="169.254.67.34")
    cam.connect()
    cam.integration_time = 100.0
    frames = cam.acquire(50)
    cam.disconnect()

:meth:`Camera.connect` handles auto-discovery when no IP is supplied,
re-connects over a stale session from a previous process, and waits for the
camera to finish cooling down. :meth:`Camera.disconnect` stops streaming,
releases GVCP control, and closes all sockets.

See also
--------
pyTelops.registers : register addresses and enum types
pyGigEVision : underlying GigE Vision protocol layer
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import socket
import struct
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress

import numpy as np

logger = logging.getLogger(__name__)

from pyGigEVision import GVCPClient, GVCPError, GVSPReceiver  # noqa: E402
from pyGigEVision.standard import (  # noqa: E402
    REG_HEARTBEAT_TIMEOUT,
    REG_SC_DEST_ADDR,
    REG_SC_HOST_PORT,
    REG_SC_PACKET_DELAY,
    REG_SC_PACKET_SIZE,
    SC_PACKET_SIZE_MASK,
    SC_SCPS_DO_NOT_FRAGMENT,
)

from . import registers as reg  # noqa: E402

# --- Enum string resolution ---
_ENUM_ALIASES = {
    reg.CalibrationMode: {
        "raw": reg.CalibrationMode.RAW,
        "raw0": reg.CalibrationMode.RAW0,
        "nuc": reg.CalibrationMode.NUC,
        "rt": reg.CalibrationMode.RT,
        "ibr": reg.CalibrationMode.IBR,
        "ibi": reg.CalibrationMode.IBI,
    },
    reg.ExposureAuto: {
        "off": reg.ExposureAuto.OFF,
        "once": reg.ExposureAuto.ONCE,
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
        raise ValueError(f"Unknown {enum_cls.__name__}: {value!r}. Valid: {valid}")
    raise TypeError(f"Expected {enum_cls.__name__}, str, or int, got {type(value).__name__}")


#: Manufacturer string Telops cameras advertise in their GVCP discovery
#: response. Used by :func:`discover` to filter out other GigE Vision
#: devices (FLIR, Basler, Allied Vision, Micro-Epsilon scanners, etc.)
#: that may share the network.
TELOPS_MANUFACTURER = "Telops Inc."


def discover(interface_ip: str = "", timeout: float = 2.0, all_vendors: bool = False) -> list[dict]:
    """Discover Telops cameras on the network.

    Sends a GVCP broadcast and collects responses from GigE Vision
    cameras. If no interface_ip is given, tries the link-local interface
    first, then broadcasts on all interfaces.

    By default only **Telops** cameras are returned — responses from
    other GigE Vision devices (FLIR, Basler, Allied Vision, laser
    scanners, etc.) that happen to share the network are filtered out
    by manufacturer string. Pass ``all_vendors=True`` to get every
    discovered GigE Vision device regardless of vendor.

    Args:
        interface_ip: Local IP to bind to (empty = auto-detect).
        timeout: Seconds to wait for responses.
        all_vendors: If True, return every GigE Vision camera found,
            not just Telops ones. Useful for debugging network setup
            when you want to see what's out there. Defaults to False.

    Returns:
        List of dicts with keys: ip, manufacturer, model,
        device_version, serial, user_name.
    """
    if interface_ip:
        cameras = GVCPClient.discover(interface_ip, timeout)
    else:
        # Try link-local first
        local_ip = _find_link_local_ip()
        if local_ip:
            cameras = GVCPClient.discover(local_ip, timeout)
            if not cameras:
                # Fallback: broadcast on all interfaces
                cameras = GVCPClient.discover("", timeout)
        else:
            cameras = GVCPClient.discover("", timeout)

    if not all_vendors:
        cameras = [c for c in cameras if c.get("manufacturer") == TELOPS_MANUFACTURER]

    return cameras


def _find_link_local_ip() -> str | None:
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
    """Telops FAST-series thermal camera over GigE Vision.

    Provides register-level control (GVCP) and frame streaming (GVSP) for
    Telops FAST infrared cameras. Supports live single-frame and burst
    acquisition, internal memory-buffer recording, hardware and software
    triggering, and a lightweight GUI viewer.

    All resolution values are in *usable pixels* (the driver hides the two
    Telops metadata rows). In ``"RT"`` calibration mode the driver
    automatically converts frames to degrees Celsius using per-frame header
    data. In ``"NUC"`` or ``"RAW"`` mode frames are raw 16-bit integers.

    Parameters
    ----------
    ip : str or None, optional
        Camera IPv4 address (e.g. ``"169.254.67.34"``). When ``None``
        (default) the driver broadcasts a GVCP discovery and uses the first
        Telops camera found on the network.
    local_ip : str or None, optional
        IPv4 address of the local network interface to use. When ``None``
        (default) the driver auto-detects the interface that can reach the
        camera.
    timeout : float, optional
        UDP socket timeout in seconds for GVCP operations. Default is
        ``2.0``.

    Examples
    --------
    Auto-discover and grab a single frame:

    >>> with Camera() as cam:
    ...     frame = cam.grab()

    Connect to a specific camera and acquire a burst:

    >>> cam = Camera(ip="169.254.67.34")
    >>> cam.connect()
    >>> cam.integration_time = 100.0
    >>> frames = cam.acquire(50)
    >>> cam.disconnect()

    Internal memory-buffer recording:

    >>> with Camera() as cam:
    ...     cam.buffer_configure(frames_per_seq=1000)
    ...     cam.buffer_arm()
    ...     cam.buffer_fire_moi()
    ...     data = cam.buffer_download()
    """

    # Number of metadata rows embedded in each frame by Telops cameras
    HEADER_ROWS = 2

    # Resolution constraints — usable pixels (excludes 2 header rows)
    WIDTH_MIN = 64
    WIDTH_MAX = 320
    WIDTH_STEP = 64
    HEIGHT_MIN = 4
    HEIGHT_MAX = 256
    HEIGHT_STEP = 4

    # Class-level registry of active Camera instances, keyed by camera IP.
    # Used to forcibly disconnect a stale instance when a new Camera
    # connects to the same camera (e.g., after a kernel restart or when
    # the user forgot to disconnect).
    _active_cameras: dict[str, Camera] = {}

    def __init__(
        self, ip: str | None = None, local_ip: str | None = None, timeout: float = 2.0
    ) -> None:
        """Initialise a Camera handle without connecting.

        Creating a :class:`Camera` object does not open any network
        connection. Call :meth:`connect` (or use the class as a context
        manager) to establish control.

        Parameters
        ----------
        ip : str or None, optional
            Camera IPv4 address. ``None`` triggers auto-discovery on
            :meth:`connect`.
        local_ip : str or None, optional
            Local interface IPv4 address. ``None`` triggers auto-detection
            on :meth:`connect`.
        timeout : float, optional
            UDP timeout in seconds for GVCP register operations. Default
            ``2.0``.
        """
        self._camera_ip = ip
        self._local_ip = local_ip or ""
        self._timeout = timeout

        self._gvcp: GVCPClient | None = None
        self._gvsp: GVSPReceiver | None = None
        self._streaming = False
        self._acquiring = False
        self._connected = False
        self._buffer_n_sequences = 1
        # Last-used buffer_configure() kwargs — used by buffer_clear() to
        # automatically re-apply the partition configuration after the
        # camera wipes it (REG_MEMORY_BUFFER_CLEAR_ALL clears both data
        # AND the partition, so the next buffer_record() would fail).
        self._buffer_config_kwargs: dict | None = None
        self._calibration_info: dict = {}
        self._calibration_names: dict = {}
        # User-set packet delay override. None = use default (force 0 on
        # start_stream for max throughput). Int = user's chosen value,
        # preserved across stream restarts.
        self._packet_delay_override: int | None = None

    def __repr__(self) -> str:
        status = "connected" if self._connected else "disconnected"
        ip = self._camera_ip or "unknown"
        return f"Camera({ip}, {status})"

    def __del__(self):
        with suppress(Exception):
            self.disconnect()

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
        """Discover the camera (if needed) and establish GVCP control.

        If no IP was supplied at construction time, a GVCP broadcast is
        sent and the first Telops camera found is used. If another
        :class:`Camera` instance in the same process is still connected to
        the same camera IP it is disconnected first, which handles the
        common "forgot to disconnect" and "kernel restart" scenarios. A
        stale CCP grant held by a process that has already exited is
        reclaimed automatically when the camera's heartbeat timeout
        expires (typically within 15 s).

        After the GVCP handshake the driver applies a set of sensible
        defaults (bad-pixel replacement on, fixed frame-rate mode, test
        image off) and, if the camera reports ``REG_DEVICE_NOT_READY``,
        blocks in :meth:`wait_until_ready` until it is ready. Idempotent
        when already connected.

        Raises
        ------
        RuntimeError
            If no Telops camera is found on the network.
        GVCPError
            If the GVCP handshake fails or a register write is rejected.

        Examples
        --------
        >>> cam = Camera()
        >>> cam.connect()
        >>> cam.is_connected
        True
        >>> cam.disconnect()
        """
        if self._connected:
            return

        # Auto-discover
        if self._camera_ip is None:
            cameras = discover(self._local_ip, self._timeout)
            if not cameras:
                # Nothing Telops found — check if there are other GigE
                # Vision devices so we can give a more specific error.
                all_cams = discover(self._local_ip, self._timeout, all_vendors=True)
                if all_cams:
                    others = ", ".join(
                        f"{c.get('manufacturer', '?')} {c.get('model', '?')}" for c in all_cams
                    )
                    raise RuntimeError(
                        f"No Telops camera found, but other GigE Vision "
                        f"devices are on the network: {others}. "
                        f"Check that the Telops camera is powered on "
                        f"and connected to the right Ethernet adapter."
                    )
                raise RuntimeError(
                    "No Telops camera found. Check:\n"
                    "  1. Camera is powered on\n"
                    "  2. Ethernet cable is connected\n"
                    "  3. No other software has GVCP control\n"
                    "  4. Firewall allows UDP for this python.exe"
                )
            self._camera_ip = cameras[0]["ip"]
            logger.info(
                "Discovered: %s %s at %s",
                cameras[0].get("manufacturer", ""),
                cameras[0].get("model", ""),
                self._camera_ip,
            )

        # If there's an existing Camera in this process connected to the
        # same camera IP, disconnect it first (handles "forgot to disconnect"
        # and "kernel restart" scenarios within the same process).
        old = Camera._active_cameras.get(self._camera_ip)
        if old is not None and old is not self and old._connected:
            logger.info("Disconnecting previous Camera instance for %s", self._camera_ip)
            with suppress(Exception):
                old.disconnect()

        # Auto-detect local IP if not specified
        if not self._local_ip:
            self._local_ip = _find_local_ip_for(self._camera_ip)

        # GVCP connection
        self._gvcp = GVCPClient(self._camera_ip, self._local_ip, self._timeout)
        self._gvcp.connect()

        # Reset heartbeat timeout
        with suppress(GVCPError):
            self._gvcp.write_reg(REG_HEARTBEAT_TIMEOUT, 3000)

        # Stop any stale acquisition left over from a previous session
        # (e.g., crash without proper disconnect)
        with suppress(GVCPError):
            self._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)

        # Clear stream destination (stop any stale streaming)
        with suppress(GVCPError):
            self._gvcp.write_reg(REG_SC_HOST_PORT, 0)

        # Prepare GVSP receiver
        self._gvsp = GVSPReceiver(self._local_ip, gvcp_client=self._gvcp)

        self._connected = True
        Camera._active_cameras[self._camera_ip] = self

        # Auto-wait if camera is not ready (cooling down, initializing, etc.)
        with suppress(GVCPError):
            if self._gvcp.read_reg(reg.REG_DEVICE_NOT_READY):
                self.wait_until_ready()

        # Apply sensible defaults (after camera is ready so writes succeed)
        with suppress(GVCPError):
            self._gvcp.write_reg(reg.REG_BAD_PIXEL_REPLACEMENT, 1)
        with suppress(GVCPError):
            self._gvcp.write_reg(reg.REG_FRAME_RATE_MODE, reg.FrameRateMode.FIXED)
        with suppress(GVCPError):
            self._gvcp.write_float(reg.REG_FRAME_RATE_MAX_FG, 1e9)
        with suppress(GVCPError):
            self._gvcp.write_reg(reg.REG_TEST_IMAGE_SELECTOR, reg.TestImageSelector.OFF)

    def wait_until_ready(self, timeout: float = 120.0, verbose: bool = True) -> None:
        """Block until the camera finishes cooling down and initialising.

        Polls ``REG_DEVICE_NOT_READY`` every two seconds. If ``verbose``
        is ``True``, the current TDC status bits (e.g. "Cooling down
        (18.5 C)") are printed on a single overwriting line so the
        terminal is not flooded. Called automatically by :meth:`connect`,
        :meth:`grab`, :meth:`acquire`, and :meth:`buffer_record` when
        the camera is not yet ready.

        Parameters
        ----------
        timeout : float, optional
            Maximum seconds to wait before raising. Default ``120.0``.
        verbose : bool, optional
            Print a live status line while waiting. Default ``True``.

        Raises
        ------
        TimeoutError
            If the camera is still not ready after *timeout* seconds.
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()

        _tdc_reasons = {
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
                    print(f"\rCamera ready. ({elapsed:.0f}s)          ", flush=True)
                return

            # Build status message
            tdc = self._gvcp.read_reg(reg.REG_TDC_STATUS)
            tdc &= ~reg.TDC_ACQUISITION_STARTED

            reasons = [desc for flag, desc in _tdc_reasons.items() if tdc & flag]
            msg = ", ".join(reasons) if reasons else "Not ready"

            if tdc & reg.TDC_WAITING_FOR_COOLER:
                try:
                    temp = self.sensor_temperature("sensor")
                    msg += f" ({temp:.1f} C)"
                except Exception:
                    pass

            elapsed = timeout - (deadline - time.monotonic())
            if verbose:
                print(f"\rWaiting: {msg} [{elapsed:.0f}s]          ", end="", flush=True)
                printed = True

            time.sleep(2.0)

        raise TimeoutError(f"Camera not ready after {timeout:.0f}s")

    @property
    def tdc_status(self) -> int:
        """Raw TDC status bitmask from ``REG_TDC_STATUS``.

        Each bit corresponds to a ``TDC_*`` constant in
        :mod:`pyTelops.registers`. Useful for diagnosing why the camera
        is not ready without waiting for the full :meth:`wait_until_ready`
        timeout.

        Returns
        -------
        int
            Bitmask of active TDC status flags.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        return self._gvcp.read_reg(reg.REG_TDC_STATUS)

    def disconnect(self) -> None:
        """Stop streaming, release GVCP control, and close all sockets.

        Calls :meth:`stop_stream` if streaming is active, closes the GVSP
        receiver, and releases the GVCP CCP grant. Idempotent when already
        disconnected. Called automatically by :meth:`__exit__` and
        :meth:`__del__`.
        """
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
        if self._camera_ip and Camera._active_cameras.get(self._camera_ip) is self:
            del Camera._active_cameras[self._camera_ip]

    @property
    def is_connected(self) -> bool:
        """Whether the camera is connected.

        Returns
        -------
        bool
            ``True`` after a successful :meth:`connect`; ``False`` after
            :meth:`disconnect` or before the first connection.
        """
        return self._connected

    @property
    def is_streaming(self) -> bool:
        """Whether GVSP streaming is active.

        Returns
        -------
        bool
            ``True`` between :meth:`start_stream` and :meth:`stop_stream`.
        """
        return self._streaming

    @property
    def is_acquiring(self) -> bool:
        """Whether continuous frame acquisition is active.

        Returns
        -------
        bool
            ``True`` between :meth:`acquisition_start` and
            :meth:`acquisition_stop`, or while inside an
            :meth:`acquisition` context manager.
        """
        return self._acquiring

    @property
    def camera_ip(self) -> str | None:
        """Camera IPv4 address, or ``None`` if not yet discovered.

        Returns
        -------
        str or None
            The address used (or to be used) for the GVCP connection.
            Set during :meth:`connect` when auto-discovery is used.
        """
        return self._camera_ip

    # ==========================================================
    # Camera Configuration (properties)
    # ==========================================================

    def _check_connected(self):
        if not self._connected:
            raise RuntimeError("Camera not connected. Call connect() first.")
        if self._gvcp and self._gvcp._control_lost:
            raise RuntimeError(
                "Camera control was lost (another application took over). "
                "Call disconnect() then connect() to re-establish."
            )

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
                UserWarning,
                stacklevel=3,
            )

    def _validate_resolution(self, w: int, h: int) -> tuple[int, int]:
        """Validate resolution in usable pixels.

        Width must be multiple of 64 (64-320).
        Height must be multiple of 4 (4-256) — usable pixels only.

        Raises ValueError with clear message if invalid.
        """
        # Width
        if w < self.WIDTH_MIN or w > self.WIDTH_MAX:
            raise ValueError(f"Width {w} out of range [{self.WIDTH_MIN}-{self.WIDTH_MAX}]")
        if w % self.WIDTH_STEP != 0:
            valid = list(range(self.WIDTH_MIN, self.WIDTH_MAX + 1, self.WIDTH_STEP))
            raise ValueError(
                f"Width must be a multiple of {self.WIDTH_STEP}. Valid widths: {valid}"
            )

        # Height (usable pixels — multiples of 4)
        if h < self.HEIGHT_MIN or h > self.HEIGHT_MAX:
            raise ValueError(f"Height {h} out of range [{self.HEIGHT_MIN}-{self.HEIGHT_MAX}]")
        if h % self.HEIGHT_STEP != 0:
            nearest = round(h / self.HEIGHT_STEP) * self.HEIGHT_STEP
            nearest = max(self.HEIGHT_MIN, min(self.HEIGHT_MAX, nearest))
            raise ValueError(
                f"Height {h} is not valid. "
                f"Min {self.HEIGHT_MIN}, max {self.HEIGHT_MAX}, step {self.HEIGHT_STEP} "
                f"(valid: 4, 8, 12, ..., 252, 256). "
                f"Nearest valid: {nearest}"
            )

        return w, h

    @property
    def valid_widths(self) -> list[int]:
        """All valid frame widths in pixels.

        Widths are multiples of :attr:`WIDTH_STEP` (64) in the range
        ``[WIDTH_MIN, WIDTH_MAX]`` i.e. ``[64, 128, 192, 256, 320]``.

        Returns
        -------
        list of int
            Sorted list of valid width values.
        """
        return list(range(self.WIDTH_MIN, self.WIDTH_MAX + 1, self.WIDTH_STEP))

    @property
    def valid_heights(self) -> list[int]:
        """All valid frame heights in usable pixels.

        Heights are multiples of :attr:`HEIGHT_STEP` (4) in the range
        ``[HEIGHT_MIN, HEIGHT_MAX]`` i.e. ``[4, 8, ..., 252, 256]``. The
        two Telops header rows are not counted here; the driver adds them
        back before writing the hardware register.

        Returns
        -------
        list of int
            Sorted list of valid height values.
        """
        return list(range(self.HEIGHT_MIN, self.HEIGHT_MAX + 1, self.HEIGHT_STEP))

    @property
    def integration_time(self) -> float:
        """Integration (exposure) time in microseconds.

        The camera-native term is exposure time; ``integration_time`` is
        the thermal-imaging convention. The two are interchangeable and
        :attr:`exposure` is kept as a backward-compatible alias.

        Returns
        -------
        float
            Current integration time in microseconds.

        Raises
        ------
        RuntimeError
            If the camera is not connected.

        Examples
        --------
        >>> with Camera() as cam:
        ...     cam.integration_time = 50.0
        ...     cam.integration_time
        50.0
        """
        self._check_connected()
        return self._gvcp.read_float(reg.REG_EXPOSURE_TIME)

    @integration_time.setter
    def integration_time(self, us: float) -> None:
        """Set the integration time in microseconds.

        If automatic exposure control (:attr:`integration_time_auto`) is
        not ``"off"``, this setter first disables AEC so the manual value
        takes effect.

        Parameters
        ----------
        us : float
            Integration time in microseconds. Must be within the range the
            camera allows for the current frame rate and resolution.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        # Disable AEC if active (it locks ExposureTime register)
        aec = self._gvcp.read_reg(reg.REG_EXPOSURE_AUTO)
        if aec != reg.ExposureAuto.OFF:
            self._gvcp.write_reg(reg.REG_EXPOSURE_AUTO, reg.ExposureAuto.OFF)
        fps_before = self._gvcp.read_float(reg.REG_ACQUISITION_FRAME_RATE)
        self._gvcp.write_float(reg.REG_EXPOSURE_TIME, us)
        self._check_fps_clamped(fps_before)

    #: Alias for :attr:`integration_time` (backward-compatible).
    exposure = integration_time

    @property
    def integration_time_auto(self) -> reg.ExposureAuto:
        """Automatic exposure control (AEC) mode.

        When not ``"off"``, the camera adjusts integration time
        automatically. Setting :attr:`integration_time` while AEC is
        active will first disable AEC.

        Returns
        -------
        reg.ExposureAuto
            Current AEC mode enum value.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        return reg.ExposureAuto(self._gvcp.read_reg(reg.REG_EXPOSURE_AUTO))

    @integration_time_auto.setter
    def integration_time_auto(self, mode: reg.ExposureAuto | str | int) -> None:
        """Set the automatic exposure control mode.

        Parameters
        ----------
        mode : reg.ExposureAuto, str, or int
            Accepted strings: ``"off"``, ``"once"``, ``"continuous"``.
            Also accepts the enum directly or its integer value.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        ValueError
            If *mode* is not a recognised string.
        """
        self._check_connected()
        self._gvcp.write_reg(reg.REG_EXPOSURE_AUTO, int(_resolve_enum(mode, reg.ExposureAuto)))

    #: Alias for :attr:`integration_time_auto` (backward-compatible).
    exposure_auto = integration_time_auto

    @property
    def frame_rate(self) -> float:
        """Acquisition frame rate in Hz.

        The camera clamps this to :attr:`frame_rate_max` whenever
        resolution or integration time changes. The setter emits a
        :class:`UserWarning` if the requested value exceeds the
        maximum for the current settings.

        Returns
        -------
        float
            Current frame rate in Hz.

        Raises
        ------
        RuntimeError
            If the camera is not connected.

        Examples
        --------
        >>> with Camera() as cam:
        ...     cam.frame_rate = 100.0
        ...     cam.frame_rate
        100.0
        """
        self._check_connected()
        return self._gvcp.read_float(reg.REG_ACQUISITION_FRAME_RATE)

    @property
    def frame_rate_max(self) -> float:
        """Maximum achievable frame rate for the current settings (Hz).

        Depends on resolution and integration time. Use this to determine
        the upper bound before setting :attr:`frame_rate`.

        Returns
        -------
        float
            Maximum frame rate in Hz.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        return self._gvcp.read_float(reg.REG_FRAME_RATE_MAX)

    @frame_rate.setter
    def frame_rate(self, hz: float) -> None:
        """Set the acquisition frame rate.

        Parameters
        ----------
        hz : float
            Desired frame rate in Hz. If *hz* exceeds :attr:`frame_rate_max`
            for the current resolution and integration time the camera clamps
            it and a :class:`UserWarning` is emitted.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
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
                UserWarning,
                stacklevel=2,
            )

    @property
    def calibration_mode(self) -> reg.CalibrationMode:
        """Active calibration pipeline mode.

        Determines what processing the camera applies to raw sensor data
        before transmitting frames over GVSP. In ``"RT"`` mode the driver
        automatically converts pixel values to degrees Celsius using the
        per-frame header coefficients.

        Returns
        -------
        reg.CalibrationMode
            Current calibration mode enum value.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        return reg.CalibrationMode(self._gvcp.read_reg(reg.REG_CALIBRATION_MODE))

    @calibration_mode.setter
    def calibration_mode(self, mode: reg.CalibrationMode | str | int) -> None:
        """Set the calibration mode.

        Parameters
        ----------
        mode : reg.CalibrationMode, str, or int
            Accepted strings: ``"RT"``, ``"NUC"``, ``"RAW"``, ``"RAW0"``,
            ``"IBR"``, ``"IBI"`` (case-insensitive). Also accepts the enum
            directly or its integer value. ``"RAW0"`` maps to
            ``CalibrationMode.RAW0`` (value 0); ``"RAW"`` maps to
            ``CalibrationMode.RAW`` (value 255). These are two distinct
            pipeline modes, not aliases of each other.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        ValueError
            If *mode* is not a recognised string.
        """
        self._check_connected()
        self._gvcp.write_reg(
            reg.REG_CALIBRATION_MODE, int(_resolve_enum(mode, reg.CalibrationMode))
        )

    @property
    def resolution(self) -> tuple[int, int]:
        """Frame resolution as ``(width, height)`` in usable pixels.

        Width must be a multiple of 64 in the range ``[64, 320]``.
        Height must be a multiple of 4 in the range ``[4, 256]``. The two
        Telops header rows are excluded from the height value here; the
        driver adds them back automatically when writing the hardware
        register. Use :attr:`valid_widths` and :attr:`valid_heights` to
        enumerate all accepted values.

        Returns
        -------
        tuple of (int, int)
            ``(width, height)`` in usable pixels.

        Raises
        ------
        RuntimeError
            If the camera is not connected.

        Examples
        --------
        >>> with Camera() as cam:
        ...     cam.resolution = (320, 256)
        ...     cam.resolution
        (320, 256)
        """
        self._check_connected()
        w = self._gvcp.read_reg(reg.REG_WIDTH)
        h = self._gvcp.read_reg(reg.REG_HEIGHT)
        return w, h - self.HEADER_ROWS

    @resolution.setter
    def resolution(self, wh: tuple[int, int]) -> None:
        """Set the frame resolution.

        Parameters
        ----------
        wh : tuple of (int, int)
            ``(width, height)`` in usable pixels. Width must be a multiple
            of 64 in ``[64, 320]``; height must be a multiple of 4 in
            ``[4, 256]``. Changing resolution may reduce the maximum frame
            rate; a :class:`UserWarning` is emitted if the current rate is
            clamped.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        ValueError
            If *width* or *height* are outside the allowed range or not a
            valid multiple.
        """
        self._check_connected()
        w, h = self._validate_resolution(wh[0], wh[1])
        fps_before = self._gvcp.read_float(reg.REG_ACQUISITION_FRAME_RATE)
        self._gvcp.write_reg(reg.REG_WIDTH, w)
        self._gvcp.write_reg(reg.REG_HEIGHT, h + self.HEADER_ROWS)
        self._check_fps_clamped(fps_before)

    @property
    def temperature(self) -> float:
        """Main camera sensor temperature in degrees Celsius (read-only).

        Reports the value from ``REG_DEVICE_TEMPERATURE``. For other
        internal temperature sensors (compressor, FPGAs, etc.) use
        :meth:`sensor_temperature`.

        Returns
        -------
        float
            Sensor temperature in degrees Celsius.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        return self._gvcp.read_float(reg.REG_DEVICE_TEMPERATURE)

    @property
    def info(self) -> dict:
        """Current camera configuration as a dictionary.

        Reads a snapshot of the most commonly needed settings in one
        call. Useful for logging state before a recording session.

        Returns
        -------
        dict
            Keys: ``ip``, ``width``, ``height``, ``integration_time_us``,
            ``integration_time_auto``, ``frame_rate_hz``,
            ``frame_rate_max_hz``, ``calibration``, ``trigger_mode``,
            ``power_state``, ``temperature_c``, ``buffer_mode``,
            ``bad_pixel_replacement``, ``reverse_x``, ``reverse_y``,
            ``test_image``, ``frame_rate_mode``, ``roi_offset``.

        Raises
        ------
        RuntimeError
            If the camera is not connected.

        Examples
        --------
        >>> with Camera() as cam:
        ...     cfg = cam.info
        ...     print(cfg["calibration"])
        RT
        """
        self._check_connected()
        return {
            "ip": self._camera_ip,
            "width": self._gvcp.read_reg(reg.REG_WIDTH),
            "height": self._gvcp.read_reg(reg.REG_HEIGHT) - self.HEADER_ROWS,
            "integration_time_us": self._gvcp.read_float(reg.REG_EXPOSURE_TIME),
            "integration_time_auto": reg.ExposureAuto(
                self._gvcp.read_reg(reg.REG_EXPOSURE_AUTO)
            ).name,
            "frame_rate_hz": self._gvcp.read_float(reg.REG_ACQUISITION_FRAME_RATE),
            "frame_rate_max_hz": self._gvcp.read_float(reg.REG_FRAME_RATE_MAX),
            "calibration": reg.CalibrationMode(self._gvcp.read_reg(reg.REG_CALIBRATION_MODE)).name,
            "trigger_mode": reg.TriggerMode(self._gvcp.read_reg(reg.REG_TRIGGER_MODE)).name,
            "power_state": reg.DevicePowerState(
                self._gvcp.read_reg(reg.REG_DEVICE_POWER_STATE)
            ).name,
            "temperature_c": self._gvcp.read_float(reg.REG_DEVICE_TEMPERATURE),
            "buffer_mode": reg.MemoryBufferMode(
                self._gvcp.read_reg(reg.REG_MEMORY_BUFFER_MODE)
            ).name,
            "bad_pixel_replacement": bool(self._gvcp.read_reg(reg.REG_BAD_PIXEL_REPLACEMENT)),
            "reverse_x": bool(self._gvcp.read_reg(reg.REG_REVERSE_X)),
            "reverse_y": bool(self._gvcp.read_reg(reg.REG_REVERSE_Y)),
            "test_image": reg.TestImageSelector(
                self._gvcp.read_reg(reg.REG_TEST_IMAGE_SELECTOR)
            ).name,
            "frame_rate_mode": reg.FrameRateMode(self._gvcp.read_reg(reg.REG_FRAME_RATE_MODE)).name,
            "roi_offset": (
                self._gvcp.read_reg(reg.REG_OFFSET_X),
                self._gvcp.read_reg(reg.REG_OFFSET_Y),
            ),
        }

    @property
    def state(self) -> str:
        """High-level camera state as a string.

        Possible values: ``"disconnected"``, ``"connected"``,
        ``"streaming"``, ``"standby"``, ``"not_ready"``, ``"error"``.

        Returns
        -------
        str
            Current camera state.
        """
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
        """Bad-pixel auto-replacement (enabled by default).

        When enabled the camera replaces pixels flagged as defective with
        the average of their neighbours before transmitting. Enabled
        automatically by :meth:`connect`. Disable only for diagnostics.

        Returns
        -------
        bool
            ``True`` if bad-pixel replacement is active.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        return bool(self._gvcp.read_reg(reg.REG_BAD_PIXEL_REPLACEMENT))

    @bad_pixel_replacement.setter
    def bad_pixel_replacement(self, enabled: bool) -> None:
        """Enable or disable bad-pixel replacement.

        Parameters
        ----------
        enabled : bool
            ``True`` to enable, ``False`` to disable.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        self._gvcp.write_reg(reg.REG_BAD_PIXEL_REPLACEMENT, int(enabled))

    @property
    def reverse_x(self) -> bool:
        """Horizontal image flip (mirror left-right).

        Returns
        -------
        bool
            ``True`` if horizontal flipping is active.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        return bool(self._gvcp.read_reg(reg.REG_REVERSE_X))

    @reverse_x.setter
    def reverse_x(self, enabled: bool) -> None:
        """Enable or disable horizontal image flip.

        Parameters
        ----------
        enabled : bool
            ``True`` to mirror the image left-right.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        self._gvcp.write_reg(reg.REG_REVERSE_X, int(enabled))

    @property
    def reverse_y(self) -> bool:
        """Vertical image flip (mirror top-bottom).

        Returns
        -------
        bool
            ``True`` if vertical flipping is active.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        return bool(self._gvcp.read_reg(reg.REG_REVERSE_Y))

    @reverse_y.setter
    def reverse_y(self, enabled: bool) -> None:
        """Enable or disable vertical image flip.

        Parameters
        ----------
        enabled : bool
            ``True`` to mirror the image top-bottom.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        self._gvcp.write_reg(reg.REG_REVERSE_Y, int(enabled))

    @property
    def test_image(self) -> reg.TestImageSelector:
        """Internal test-pattern source.

        Use ``"off"`` (the default set by :meth:`connect`) for normal
        operation. Test patterns are useful for verifying streaming
        without a physical scene.

        Returns
        -------
        reg.TestImageSelector
            Current test image selector enum value.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        return reg.TestImageSelector(self._gvcp.read_reg(reg.REG_TEST_IMAGE_SELECTOR))

    @test_image.setter
    def test_image(self, mode: reg.TestImageSelector | str | int) -> None:
        """Select the internal test-pattern source.

        Parameters
        ----------
        mode : reg.TestImageSelector, str, or int
            Accepted strings: ``"off"``, ``"static"``, ``"dynamic"``,
            ``"constant"`` (case-insensitive). Also accepts the enum
            directly or its integer value.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        ValueError
            If *mode* is not a recognised string.
        """
        self._check_connected()
        self._gvcp.write_reg(
            reg.REG_TEST_IMAGE_SELECTOR, int(_resolve_enum(mode, reg.TestImageSelector))
        )

    @property
    def roi_offset(self) -> tuple[int, int]:
        """Region-of-interest pixel offset as ``(x, y)``.

        Defines the top-left corner of the active area on the sensor.
        ``x`` must be a non-negative multiple of :attr:`WIDTH_STEP` (64);
        ``y`` must be a non-negative multiple of :attr:`HEIGHT_STEP` (4).
        The combination of offset and :attr:`resolution` must not exceed
        the full sensor size (320 x 256).

        Returns
        -------
        tuple of (int, int)
            ``(x, y)`` pixel offset from the top-left of the sensor.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        return (self._gvcp.read_reg(reg.REG_OFFSET_X), self._gvcp.read_reg(reg.REG_OFFSET_Y))

    @roi_offset.setter
    def roi_offset(self, xy: tuple[int, int]) -> None:
        """Set the region-of-interest pixel offset.

        Parameters
        ----------
        xy : tuple of (int, int)
            ``(x, y)`` offset in pixels. Both values must be non-negative.
            ``x`` must be a multiple of 64; ``y`` must be a multiple of 4.
            The subwindow ``(x + width, y + height)`` must fit within the
            320 x 256 sensor.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        ValueError
            If the offset is out of range, not properly aligned, or the
            subwindow exceeds the sensor dimensions.
        """
        self._check_connected()
        x, y = int(xy[0]), int(xy[1])

        # Validate alignment client-side so users get a clear error
        # instead of a cryptic GVCP GENERIC_ERROR from the camera.
        if x < 0 or x % self.WIDTH_STEP != 0:
            raise ValueError(
                f"roi_offset x={x} is invalid. Must be a non-negative "
                f"multiple of {self.WIDTH_STEP} (the width step). "
                f"Valid values: 0, {self.WIDTH_STEP}, "
                f"{2 * self.WIDTH_STEP}, ..."
            )
        if y < 0 or y % self.HEIGHT_STEP != 0:
            raise ValueError(
                f"roi_offset y={y} is invalid. Must be a non-negative "
                f"multiple of {self.HEIGHT_STEP} (the height step). "
                f"Valid values: 0, {self.HEIGHT_STEP}, "
                f"{2 * self.HEIGHT_STEP}, ..."
            )

        # Validate that subwindow fits within the sensor
        w, h = self.resolution
        if x + w > self.WIDTH_MAX:
            raise ValueError(
                f"roi_offset x={x} + width={w} = {x + w} exceeds "
                f"sensor width {self.WIDTH_MAX}. Reduce resolution or "
                f"offset."
            )
        if y + h > self.HEIGHT_MAX:
            raise ValueError(
                f"roi_offset y={y} + height={h} = {y + h} exceeds "
                f"sensor height {self.HEIGHT_MAX}. Reduce resolution or "
                f"offset."
            )

        self._gvcp.write_reg(reg.REG_OFFSET_X, x)
        self._gvcp.write_reg(reg.REG_OFFSET_Y, y)

    @property
    def frame_rate_mode(self) -> reg.FrameRateMode:
        """Frame-rate control mode.

        Controls how the camera determines its output frame rate.

        * ``"fixed"`` -- use the value in :attr:`frame_rate` (default set
          by :meth:`connect`).
        * ``"fixed_locked"`` -- locked to an external timing source.
        * ``"maximum"`` -- always run at :attr:`frame_rate_max`.
        * ``"burst"`` -- burst mode (used with trigger frame count).

        Returns
        -------
        reg.FrameRateMode
            Current frame-rate mode enum value.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        return reg.FrameRateMode(self._gvcp.read_reg(reg.REG_FRAME_RATE_MODE))

    @frame_rate_mode.setter
    def frame_rate_mode(self, mode: reg.FrameRateMode | str | int) -> None:
        """Set the frame-rate control mode.

        Parameters
        ----------
        mode : reg.FrameRateMode, str, or int
            Accepted strings: ``"fixed"``, ``"fixed_locked"``
            (alias ``"locked"``), ``"maximum"`` (alias ``"max"``),
            ``"burst"`` (case-insensitive). Also accepts the enum directly
            or its integer value.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        ValueError
            If *mode* is not a recognised string.
        """
        self._check_connected()
        self._gvcp.write_reg(reg.REG_FRAME_RATE_MODE, int(_resolve_enum(mode, reg.FrameRateMode)))

    @property
    def trigger_frame_count(self) -> int:
        """Number of frames captured per trigger event.

        Relevant when :attr:`frame_rate_mode` is ``"burst"`` or when
        using :meth:`trigger_software`. The camera emits this many
        frames after each trigger pulse.

        Returns
        -------
        int
            Current trigger frame count.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        return self._gvcp.read_reg(reg.REG_TRIGGER_FRAME_COUNT)

    @trigger_frame_count.setter
    def trigger_frame_count(self, count: int) -> None:
        """Set the number of frames per trigger event.

        Parameters
        ----------
        count : int
            Number of frames to capture per trigger.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        self._gvcp.write_reg(reg.REG_TRIGGER_FRAME_COUNT, count)

    @property
    def packet_delay(self) -> int:
        """Inter-packet delay for GVSP streaming in camera timer ticks.

        Each tick is 8 ns on Telops cameras. The camera inserts this
        much delay between successive packets within one frame burst,
        which spreads the data over time and reduces host-side UDP
        receive overflow at the cost of slightly lower maximum frame
        rate.

        Typical values:

        * ``0`` -- no delay (default). Maximum throughput. Works on
          clean networks with a fast receiver. Risk of packet loss if
          the host has scheduling jitter (GC, display redraws).
        * ``1000`` -- ~8 us between packets. Spreads a 113-packet
          frame over ~2 ms. Usable up to ~400 fps. Safe default for
          live processing loops with non-trivial host work.
        * ``5000`` -- ~40 us between packets. Very conservative,
          max ~100 fps. Use only if ``1000`` is not enough.

        Writing this property sends the new value to the camera
        register immediately and caches it so it is re-applied on
        subsequent :meth:`start_stream` calls, surviving stream
        restarts and context-manager re-entry.

        This setting has no effect on internal memory-buffer recording
        or buffer download, which use separate bitrate registers.

        Returns
        -------
        int
            Current inter-packet delay in camera timer ticks.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        return self._gvcp.read_reg(REG_SC_PACKET_DELAY)

    @packet_delay.setter
    def packet_delay(self, ticks: int) -> None:
        """Set the inter-packet delay for GVSP streaming.

        Parameters
        ----------
        ticks : int
            Non-negative number of camera timer ticks (8 ns each) to
            insert between successive stream packets. Set to ``0`` for
            maximum throughput; increase if packets are dropped on busy
            hosts.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        ValueError
            If *ticks* is negative.
        """
        self._check_connected()
        ticks = int(ticks)
        if ticks < 0:
            raise ValueError(f"packet_delay must be non-negative, got {ticks}")
        self._gvcp.write_reg(REG_SC_PACKET_DELAY, ticks)
        self._packet_delay_override = ticks

    # ==========================================================
    # Streaming
    # ==========================================================

    def start_stream(self) -> None:
        """Configure the GVSP stream channel and start the receiver thread.

        Sets the packet size to the standard MTU (1500 bytes), writes the
        destination IP and port to the camera, and starts the GVSP receiver.
        Idempotent -- safe to call when streaming is already active.

        After this call, frames flow from the camera but are not queued in the
        acquisition buffer until :meth:`acquisition_start` is also called.
        For a single combined start, use :meth:`acquisition_start` directly
        (it calls :meth:`start_stream` automatically).

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        if self._streaming:
            return

        # Clamp packet size to standard MTU, preserving flag bits
        target_pkt_size = 1500
        pkt_reg = self._gvcp.read_reg(REG_SC_PACKET_SIZE)
        current_size = pkt_reg & SC_PACKET_SIZE_MASK
        if current_size != target_pkt_size:
            # Preserve upper flags and lower flag bits (DoNotFragment etc.)
            non_size_bits = pkt_reg & ~SC_PACKET_SIZE_MASK
            self._gvcp.write_reg(REG_SC_PACKET_SIZE, non_size_bits | target_pkt_size)

        self._gvsp._packet_data_size = target_pkt_size - 8

        # Inter-packet delay: respect user override if set, otherwise
        # force to 0 for maximum throughput (original default behavior).
        try:
            target_delay = (
                self._packet_delay_override if self._packet_delay_override is not None else 0
            )
            current_delay = self._gvcp.read_reg(REG_SC_PACKET_DELAY)
            if current_delay != target_delay:
                self._gvcp.write_reg(REG_SC_PACKET_DELAY, target_delay)
        except GVCPError:
            pass

        # Tell camera where to send stream data
        sock_ip = self._gvsp._sock.getsockname()[0]
        if not sock_ip or sock_ip == "0.0.0.0":
            sock_ip = self._local_ip
        ip_int = struct.unpack(">I", socket.inet_aton(sock_ip))[0]

        self._gvcp.write_reg(REG_SC_DEST_ADDR, ip_int)
        self._gvcp.write_reg(REG_SC_HOST_PORT, self._gvsp.port)

        self._gvsp.start()
        self._streaming = True

    def stop_stream(self) -> None:
        """Stop the GVSP receiver and tear down the stream channel.

        If acquisition is currently running, :meth:`acquisition_stop` is
        called first. Then the camera's host-port register is cleared and the
        GVSP receiver thread is stopped. Idempotent -- safe to call when
        streaming is already inactive.

        To pause acquisition without releasing the socket (so the next
        :meth:`acquisition_start` is faster), use :meth:`acquisition_stop`
        alone instead of this method.
        """
        if not self._streaming:
            return

        # If acquisition is still running, stop it first
        if self._acquiring:
            self.acquisition_stop()

        with suppress(GVCPError):
            self._gvcp.write_reg(REG_SC_HOST_PORT, 0)

        self._gvsp.stop()
        self._streaming = False

    # ==========================================================
    # Frame Acquisition
    # ==========================================================

    # Header byte offsets for per-frame calibration data
    _HDR_DATA_OFFSET = 12  # float32: additive offset (273.15 for RT Kelvin)
    _HDR_DATA_EXP = 16  # int8: exponent (typically -8 for RT)
    _HDR_CAL_MODE = 28  # uint8: calibration mode (2=RT, 1=NUC, etc.)

    def _strip_headers(self, arr: np.ndarray) -> np.ndarray:
        """Strip Telops header rows from a frame or batch of frames."""
        if self.HEADER_ROWS == 0:
            return arr
        if arr.ndim == 2:
            return arr[self.HEADER_ROWS :, :]
        elif arr.ndim == 3:
            return arr[:, self.HEADER_ROWS :, :]
        return arr

    def _apply_calibration(self, frame: np.ndarray) -> np.ndarray:
        """Apply per-frame calibration using the embedded Telops header data.

        Reads ``DataExp`` and ``DataOffset`` from the header rows (which must
        still be present in *frame*) and converts pixel values::

            physical = pixel * 2**DataExp + DataOffset
            # RT mode only: physical -= 273.15  (Kelvin to Celsius)

        For NUC/RAW frames ``DataExp`` and ``DataOffset`` are both zero, so
        the function strips headers and returns without arithmetic conversion.

        Parameters
        ----------
        frame : numpy.ndarray
            Raw frame (2-D) or batch (3-D) WITH the two Telops header rows
            still included (i.e. shape ``(H+2, W)`` or ``(N, H+2, W)``).

        Returns
        -------
        numpy.ndarray
            float32 array with header rows stripped. Returns the unmodified
            uint16 sub-array (header stripped) when no calibration is needed.
        """
        if frame.ndim == 2:
            header_bytes = frame[: self.HEADER_ROWS, :].tobytes()
            data_exp = struct.unpack(
                "<b", header_bytes[self._HDR_DATA_EXP : self._HDR_DATA_EXP + 1]
            )[0]
            data_offset = struct.unpack(
                "<f", header_bytes[self._HDR_DATA_OFFSET : self._HDR_DATA_OFFSET + 4]
            )[0]
            cal_mode = header_bytes[self._HDR_CAL_MODE]

            if data_exp == 0 and data_offset == 0:
                return frame[self.HEADER_ROWS :, :]  # strip headers, no conversion

            data = frame[self.HEADER_ROWS :, :].astype(np.float32)
            data = data * (2.0**data_exp) + data_offset

            # RT mode: convert Kelvin to Celsius
            if cal_mode == 2:  # RT
                data -= 273.15

            return data

        elif frame.ndim == 3:
            # Batch: apply per-frame
            results = []
            for i in range(frame.shape[0]):
                results.append(self._apply_calibration(frame[i]))
            return np.stack(results)

        return frame

    # ==========================================================
    # Continuous Acquisition
    # ==========================================================

    def acquisition_start(self) -> None:
        """Start continuous frame acquisition.

        Calls :meth:`start_stream` if the GVSP channel is not yet open, then
        writes the acquisition-start register. Frames begin flowing and can
        be retrieved with :meth:`read_frame`. Idempotent -- safe to call when
        already acquiring.

        Pair with :meth:`acquisition_stop` to halt. For automatic cleanup on
        exception, prefer the :meth:`acquisition` context manager::

            with cam.acquisition():
                while running:
                    frame = cam.read_frame(timeout=0.1)
                    if frame is not None:
                        process(frame)

        Raises
        ------
        RuntimeError
            If the camera is not connected or not ready.

        Notes
        -----
        Not thread-safe. Acquisition lifecycle calls
        (``acquisition_start`` / ``acquisition_stop``) must be serialized by
        the caller. :meth:`read_frame` is safe to call concurrently with
        itself from multiple threads.
        """
        self._check_ready()
        if self._acquiring:
            return
        if not self._streaming:
            self.start_stream()
        self._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)
        self._acquiring = True

    def acquisition_stop(self) -> None:
        """Stop continuous frame acquisition.

        Writes the acquisition-stop register and clears the internal
        ``_acquiring`` flag. The GVSP stream channel is left intact so a
        subsequent :meth:`acquisition_start` can resume without re-binding
        sockets. For a full teardown (including the receiver thread), call
        :meth:`stop_stream`. Idempotent -- safe to call when not acquiring.

        Notes
        -----
        Not thread-safe. Acquisition lifecycle calls
        (``acquisition_start`` / ``acquisition_stop``) must be serialized by
        the caller. :meth:`read_frame` is safe to call concurrently from
        multiple threads.
        """
        if not self._acquiring:
            return
        try:
            self._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)
        except GVCPError as e:
            logger.warning("Failed to write REG_ACQUISITION_STOP: %s", e)
        self._acquiring = False

    @contextmanager
    def acquisition(self) -> Iterator[Camera]:
        """Context manager for continuous frame acquisition.

        Calls :meth:`acquisition_start` on entry and :meth:`acquisition_stop`
        on exit, even if an exception is raised inside the block. The GVSP
        stream channel is left open after exit so a subsequent
        :meth:`acquisition_start` (or ``with cam.acquisition()``) can resume
        without re-binding sockets.

        Yields
        ------
        Camera
            The camera instance itself, for fluent use inside the block.

        Raises
        ------
        RuntimeError
            If the camera is not connected or not ready (propagated from
            :meth:`acquisition_start`).

        Examples
        --------
        >>> with cam.acquisition() as c:
        ...     for _ in range(100):
        ...         frame = c.read_frame(timeout=0.1)
        ...         if frame is not None:
        ...             process(frame)
        """
        self.acquisition_start()
        try:
            yield self
        finally:
            self.acquisition_stop()

    def read_frame(
        self,
        timeout: float = 0.0,
        strip_header: bool = True,
        convert: bool = True,
        latest: bool = False,
    ) -> np.ndarray | None:
        """Pop one frame from the acquisition queue.

        Must be called after :meth:`acquisition_start` or inside an
        :meth:`acquisition` block. Non-blocking by default.

        Parameters
        ----------
        timeout : float, optional
            Seconds to wait for a frame. ``0.0`` (default) returns
            immediately with whatever is queued, or ``None`` if empty.
            Use a small positive value (e.g. ``0.1``) to block briefly.
        strip_header : bool, optional
            Remove the two Telops metadata rows. Default ``True``. When
            ``convert=True`` the headers are consumed by the calibration
            step regardless of this flag.
        convert : bool, optional
            Convert pixel values to physical units using the per-frame
            header coefficients. In RT mode this yields float32 degrees
            Celsius; in other modes the conversion is still applied but
            the result is in the camera's native units. Set ``False`` to
            get raw uint16 counts. Default ``True``.
        latest : bool, optional
            When ``True``, drain the queue and return only the newest
            frame, discarding stale ones. Use for live displays where lag
            must not accumulate. When ``False`` (default), return frames
            in order -- the correct choice for measurement and logging.

        Returns
        -------
        numpy.ndarray or None
            2-D array of shape ``(H, W)``. dtype is float32 when
            ``convert=True`` (RT mode), uint16 otherwise. Returns
            ``None`` if no frame was available within *timeout*.

        Raises
        ------
        RuntimeError
            If acquisition is not currently running.

        Notes
        -----
        Thread-safe -- multiple threads may call :meth:`read_frame`
        concurrently. The underlying GVSP frame queue handles
        inter-thread coordination.

        Examples
        --------
        Non-blocking poll inside a loop:

        >>> cam.acquisition_start()
        >>> frame = cam.read_frame(timeout=2.0)
        >>> cam.acquisition_stop()

        Live-display loop with stale-frame draining:

        >>> with cam.acquisition():
        ...     while displaying:
        ...         frame = cam.read_frame(timeout=0.05, latest=True)
        ...         if frame is not None:
        ...             show(frame)
        """
        if not self._acquiring:
            raise RuntimeError(
                "Camera acquisition not active. Call cam.acquisition_start() "
                "or use 'with cam.acquisition():' before read_frame()."
            )

        if latest:
            # Drain the queue non-blocking, keeping only the newest frame
            frame = None
            while True:
                newer = self._gvsp.get_frame(timeout=0.0)
                if newer is None:
                    break
                frame = newer
            # If the queue was empty, block briefly for a fresh frame
            if frame is None and timeout > 0.0:
                frame = self._gvsp.get_frame(timeout=timeout)
        else:
            frame = self._gvsp.get_frame(timeout=timeout)

        if frame is None:
            return None
        if convert:
            frame = self._apply_calibration(frame)
        elif strip_header:
            frame = self._strip_headers(frame)
        return frame

    # ==========================================================
    # Single-shot / Batch Acquisition
    # ==========================================================

    def grab(
        self, timeout: float = 5.0, strip_header: bool = True, convert: bool = True
    ) -> np.ndarray | None:
        """Grab a single frame.

        Convenience wrapper for one-shot acquisition. If streaming or
        acquisition are not already active they are started and then
        restored to their prior state after the frame is captured. For
        repeated access in a loop, prefer :meth:`acquisition` +
        :meth:`read_frame` -- this method carries per-call setup overhead.

        Parameters
        ----------
        timeout : float, optional
            Seconds to wait for a frame. Default ``5.0``.
        strip_header : bool, optional
            Remove the two Telops metadata rows. Default ``True``. Ignored
            when ``convert=True`` because the calibration step handles
            header removal.
        convert : bool, optional
            Apply per-frame calibration. In RT mode this yields float32
            degrees Celsius. Set ``False`` for raw uint16 counts.
            Default ``True``.

        Returns
        -------
        numpy.ndarray or None
            2-D array of shape ``(H, W)``. dtype is float32 when
            ``convert=True`` (RT mode), uint16 otherwise. Returns
            ``None`` on timeout.

        Raises
        ------
        RuntimeError
            If the camera is not connected or not ready.

        Examples
        --------
        >>> with Camera() as cam:
        ...     frame = cam.grab()  # float32 Celsius, RT mode
        ...     raw = cam.grab(convert=False)  # uint16 raw counts
        """
        self._check_ready()
        was_streaming = self._streaming
        was_acquiring = self._acquiring

        try:
            if not self._acquiring:
                self.acquisition_start()
            frame = self._gvsp.get_frame(timeout=timeout)
        finally:
            if not was_acquiring:
                self.acquisition_stop()
            if not was_streaming:
                self.stop_stream()

        if frame is not None:
            if convert:
                frame = self._apply_calibration(frame)
            elif strip_header:
                frame = self._strip_headers(frame)
        return frame

    def acquire(
        self, n_frames: int, timeout: float = 30.0, strip_header: bool = True, convert: bool = True
    ) -> np.ndarray | None:
        """Acquire a burst of frames via live streaming.

        Starts streaming and acquisition if not already active, collects
        exactly *n_frames* frames (or as many as arrive before *timeout*
        expires), then restores the prior state.

        Parameters
        ----------
        n_frames : int
            Number of frames to capture.
        timeout : float, optional
            Total wall-clock timeout in seconds across all frames.
            Default ``30.0``.
        strip_header : bool, optional
            Remove the two Telops metadata rows. Default ``True``. Ignored
            when ``convert=True`` because the calibration step handles
            header removal.
        convert : bool, optional
            Apply per-frame calibration. In RT mode this yields float32
            degrees Celsius. Set ``False`` for raw uint16 counts.
            Default ``True``.

        Returns
        -------
        numpy.ndarray or None
            3-D array of shape ``(N, H, W)`` where *N* is the number of
            frames actually received (may be less than *n_frames* on
            timeout). dtype is float32 when ``convert=True`` (RT mode),
            uint16 otherwise. Returns ``None`` if no frames were captured.

        Raises
        ------
        RuntimeError
            If the camera is not connected or not ready.
        """
        self._check_ready()
        was_streaming = self._streaming
        was_acquiring = self._acquiring

        frames = []
        try:
            if not self._acquiring:
                self.acquisition_start()
            deadline = time.monotonic() + timeout
            for _ in range(n_frames):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                result = self._gvsp.get_frame(timeout=remaining)
                if result is not None:
                    frames.append(result)
        finally:
            if not was_acquiring:
                self.acquisition_stop()
            if not was_streaming:
                self.stop_stream()

        if not frames:
            return None
        result = np.stack(frames)
        if convert:
            result = self._apply_calibration(result)
        elif strip_header:
            result = self._strip_headers(result)
        return result

    # ==========================================================
    # Trigger
    # ==========================================================

    def configure_trigger(
        self,
        source: reg.TriggerSource | str | int = "external",
        activation: reg.TriggerActivation | str | int = "rising",
        selector: reg.TriggerSelector | str | int = "acquisition_start",
        enabled: bool = True,
    ) -> None:
        """Configure the hardware or software trigger.

        Writes the trigger selector, source, activation, and mode registers
        in a single call. After this, the camera waits for a trigger before
        each frame (or acquisition start, depending on *selector*).

        Parameters
        ----------
        source : reg.TriggerSource, str, or int, optional
            Trigger source. Accepted strings (case-insensitive):
            ``"external"`` -- BNC connector (default),
            ``"software"`` -- :meth:`software_trigger`. Also accepts the
            :class:`reg.TriggerSource` enum directly or its integer value.
        activation : reg.TriggerActivation, str, or int, optional
            Edge or level that fires the trigger. Accepted strings:
            ``"rising"`` (default), ``"falling"``, ``"any"``. Also accepts
            :class:`reg.TriggerActivation` directly or its integer value.
            ``"level_high"`` and ``"level_low"`` are available as enum
            members but have no alias string -- pass the enum or integer.
        selector : reg.TriggerSelector, str, or int, optional
            Which camera event the trigger controls. Accepted strings:
            ``"acquisition_start"`` (default), ``"flagging"``,
            ``"gating"``. Also accepts :class:`reg.TriggerSelector`
            directly or its integer value.
        enabled : bool, optional
            ``True`` (default) to enable trigger mode; ``False`` to disable
            it (free-running).

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        ValueError
            If *source*, *activation*, or *selector* is an unrecognised
            string.
        """
        self._check_connected()
        self._gvcp.write_reg(
            reg.REG_TRIGGER_SELECTOR, int(_resolve_enum(selector, reg.TriggerSelector))
        )
        self._gvcp.write_reg(reg.REG_TRIGGER_SOURCE, int(_resolve_enum(source, reg.TriggerSource)))
        self._gvcp.write_reg(
            reg.REG_TRIGGER_ACTIVATION, int(_resolve_enum(activation, reg.TriggerActivation))
        )
        self._gvcp.write_reg(
            reg.REG_TRIGGER_MODE, int(reg.TriggerMode.ON if enabled else reg.TriggerMode.OFF)
        )

    def software_trigger(self) -> None:
        """Send a software trigger pulse to the camera.

        Writes ``1`` to ``REG_TRIGGER_SOFTWARE``. Has effect only when
        trigger mode is enabled and the source is set to ``"software"``
        (see :meth:`configure_trigger`).

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        self._gvcp.write_reg(reg.REG_TRIGGER_SOFTWARE, 1)

    # ==========================================================
    # Image Processing
    # ==========================================================

    def nuc(
        self,
        mode: str | reg.ImageCorrectionMode = "black_body",
        blackbody_temp: float | None = None,
        timeout: float = 60.0,
    ) -> None:
        """Perform Non-Uniformity Correction (NUC).

        Writes the correction mode and (optionally) the external blackbody
        temperature, then triggers the NUC sequence and polls
        ``REG_DEVICE_NOT_READY`` once per second until the camera reports
        ready.  Many registers are locked by the camera during this time.

        Parameters
        ----------
        mode : str or reg.ImageCorrectionMode, optional
            Correction algorithm.  Accepted strings (case-insensitive):
            ``"black_body"`` (default) or ``"icu"``.  Also accepts the
            :class:`reg.ImageCorrectionMode` enum directly.
        blackbody_temp : float or None, optional
            External blackbody reference temperature in degrees Celsius.
            Used only in ``"black_body"`` mode.  When ``None`` (default)
            the register is not written and the camera uses whatever value
            was previously programmed.
        timeout : float, optional
            Maximum seconds to wait for the NUC to finish.  Default 60.0.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        TimeoutError
            If the NUC sequence does not complete within *timeout* seconds.
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

    def sensor_temperature(self, location: str | reg.TemperatureLocation = "sensor") -> float:
        """Read the temperature at a specific camera location.

        Writes the selector register then reads the float result register.
        For a snapshot of all locations at once, use :meth:`diagnostics`.

        Parameters
        ----------
        location : str or reg.TemperatureLocation, optional
            Sensor location identifier.  Accepted strings (case-insensitive):
            ``"sensor"``, ``"mainboard"``, ``"internal_lens"``,
            ``"external_lens"``, ``"icu"``, ``"filter_wheel"``,
            ``"compressor"``, ``"cold_finger"``, ``"spare"``,
            ``"external_thermistor"``, ``"processing_fpga"``,
            ``"output_fpga"``, ``"storage_fpga"``.  Defaults to
            ``"sensor"``.  Also accepts the :class:`reg.TemperatureLocation`
            enum or its integer value.

        Returns
        -------
        float
            Temperature in degrees Celsius.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        ValueError
            If *location* is not a recognised string.
        """
        self._check_connected()
        loc = _resolve_enum(location, reg.TemperatureLocation)
        self._gvcp.write_reg(reg.REG_DEVICE_TEMPERATURE_SELECTOR, int(loc))
        return self._gvcp.read_float(reg.REG_DEVICE_TEMPERATURE_READOUT)

    def diagnostics(self) -> dict:
        """Read all diagnostic sensors in one call.

        Iterates over every entry in :class:`reg.TemperatureLocation`,
        :class:`reg.VoltageLocation`, and :class:`reg.CurrentLocation`,
        writing each selector register and reading the corresponding float
        register.  Sensors that the camera rejects with a
        :class:`GVCPError` (unsupported on this model) are stored as
        ``None``.  The call issues roughly 40 register reads and typically
        completes within a few hundred milliseconds.

        Returns
        -------
        dict
            A dict with the following keys:

            ``"temperatures"`` : dict[str, float or None]
                Keyed by lowercase :class:`reg.TemperatureLocation` member
                names (e.g. ``"sensor"``, ``"compressor"``,
                ``"cold_finger"``, ``"processing_fpga"``).  Values in
                degrees Celsius; ``None`` if the location is not supported
                by this camera model.
            ``"voltages"`` : dict[str, float or None]
                Keyed by lowercase :class:`reg.VoltageLocation` member
                names (e.g. ``"cooler"``, ``"supply_24v"``).  Values in
                volts; ``None`` if unsupported.
            ``"currents"`` : dict[str, float or None]
                Keyed by lowercase :class:`reg.CurrentLocation` member
                names (e.g. ``"cooler"``, ``"supply_24v"``).  Values in
                amps; ``None`` if unsupported.
            ``"device_running_s"`` : int
                Total device uptime in seconds.
            ``"cooler_running_s"`` : int
                Total cooler uptime in seconds.
            ``"power_on_cycles"`` : int
                Number of times the device has been powered on.
            ``"cooler_power_on_cycles"`` : int
                Number of times the cooler has been powered on.

        Raises
        ------
        RuntimeError
            If the camera is not connected.

        Examples
        --------
        >>> d = cam.diagnostics()
        >>> print(d["temperatures"]["sensor"])
        -196.3
        >>> print(d["voltages"]["cooler"])
        4.85
        >>> print(d["device_running_s"] / 3600, "hours")
        12.4 hours
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
        """Save the current configuration to camera non-volatile memory.

        Writes ``1`` to ``REG_SAVE_CONFIGURATION``.  The camera persists
        all writable registers (integration time, resolution, frame rate,
        trigger settings, etc.) so they are restored on next power-on.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        self._gvcp.write_reg(reg.REG_SAVE_CONFIGURATION, 1)

    def sync_time(self) -> None:
        """Synchronise the camera clock to the host system time (UTC).

        Reads the current UTC time from the host and writes the integer
        POSIX timestamp to ``REG_POSIX_TIME``.  Sub-second precision is
        not written; use the :attr:`posix_time` setter with a
        :class:`datetime.datetime` object for finer control.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        import datetime

        self._check_connected()
        now = datetime.datetime.now(datetime.timezone.utc)
        self._gvcp.write_reg(reg.REG_POSIX_TIME, int(now.timestamp()))

    @property
    def posix_time(self) -> datetime.datetime:
        """Camera wall-clock time as a timezone-aware UTC datetime.

        Reads ``REG_POSIX_TIME`` (whole seconds) and ``REG_SUB_SECOND_TIME``
        (100-nanosecond ticks) from the camera and combines them into a
        :class:`datetime.datetime` with microsecond resolution.

        Returns
        -------
        datetime.datetime
            Timezone-aware datetime in UTC with microsecond precision.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        seconds = self._gvcp.read_reg(reg.REG_POSIX_TIME)
        sub_100ns = self._gvcp.read_reg(reg.REG_SUB_SECOND_TIME)
        microseconds = sub_100ns // 10  # 100ns ticks -> microseconds
        return datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc).replace(
            microsecond=microseconds
        )

    @posix_time.setter
    def posix_time(self, dt: datetime.datetime | float | int) -> None:
        """Set the camera clock from a datetime object or POSIX timestamp.

        Parameters
        ----------
        dt : datetime.datetime or float or int
            When *dt* has a ``timestamp()`` method (i.e. is a
            :class:`datetime.datetime`), its integer POSIX timestamp is
            written to ``REG_POSIX_TIME``.  Otherwise the value itself is
            cast to ``int`` and written directly (interpreted as seconds
            since the Unix epoch).  Sub-second precision is truncated.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        if hasattr(dt, "timestamp"):
            self._gvcp.write_reg(reg.REG_POSIX_TIME, int(dt.timestamp()))
        else:
            self._gvcp.write_reg(reg.REG_POSIX_TIME, int(dt))

    @property
    def gev_timestamp_ns(self) -> int:
        """GigE Vision free-running timestamp in nanoseconds (read-only).

        Latches the camera's GigE Vision internal counter by writing
        ``2`` to ``REG_GEV_TIMESTAMP_CONTROL``, then reads the 64-bit
        tick value from the high and low word registers and the tick
        frequency.  The tick count is scaled to nanoseconds as
        ``ticks * 1_000_000_000 / freq``.  If the camera reports a
        frequency of zero the raw tick count is returned unchanged.

        This timestamp is independent of wall-clock time; use
        :attr:`posix_time` or :meth:`sync_time` for UTC-anchored time.

        Returns
        -------
        int
            Elapsed time since the camera was powered on, in nanoseconds.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
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
    # Calibration
    # ==========================================================

    @property
    def calibration_names(self) -> dict[int, str]:
        """Manual name mapping ``{collection_index: name}`` for calibration collections.

        Provides human-readable labels that appear in the output of
        :meth:`calibration_collections`, :meth:`calibration_load`, and
        :meth:`calibration_active` without requiring the USB calibration
        data directory.  A connection to the camera is not needed to set
        this mapping.

        Returns
        -------
        dict[int, str]
            Current ``{index: name}`` mapping (empty by default).

        See Also
        --------
        load_calibration_info : Load lens/temperature metadata from the
            calibration data directory on USB.
        """
        return self._calibration_names

    @calibration_names.setter
    def calibration_names(self, names: dict[int, str]) -> None:
        """Set the manual name mapping for calibration collections.

        Parameters
        ----------
        names : dict[int, str]
            Mapping of collection index (0-based) to a human-readable name
            string.  For example ``{0: "MW 50mm FW1", 3: "Microscope FW2"}``.
        """
        self._calibration_names = names

    def load_calibration_info(self, path: str) -> None:
        """Load calibration metadata from the USB calibration data directory.

        Parses ``.tsco`` filenames and exposure-time text files to build a
        mapping from camera collection indices to lens names, filter-wheel
        positions, and calibrated temperature ranges.  A camera connection
        is not required -- the method reads only local files.  When
        connected, indices are resolved immediately; when called before
        :meth:`connect`, the raw file data is stored and matched on
        connection.

        After this call, :meth:`calibration_collections` will include
        ``"lens"``, ``"fw_position"``, and ``"temp_range"`` fields where
        available, and :meth:`calibration_load` supports the
        ``lens=`` / ``temp=`` selection path.

        Directory layout expected
        -------------------------
        ``<path>/``
            One or more ``.tsco`` files whose names encode the sensor
            serial number, EL identifier, filter-wheel position (FW),
            and a POSIX timestamp.  Two filename formats are supported:

            * Old: ``TEL08050_<POSIX>_ELxxxxx_MFxxxxx_FWn_IMn_SWDn.tsco``
            * New: ``TEL08050_ELxxxxx_MFxxxxx_FWn_IMn_SWDn_<POSIX>.tsco``

        ``<path>/estimated_ExposureTimes/``
            Optional sub-directory of ``.txt`` files.  Each file contains
            a header line with the lens name (``lens "MW 50mm"``) and the
            filter-wheel position (``filter wheel position #N``), followed
            by semicolon-delimited temperature/exposure rows.  The first
            column of the first and last data rows gives ``temp_min`` and
            ``temp_max`` for the collection.

        Naming normalisation
        --------------------
        Exposure-time filenames may use ``ELSN`` as the element prefix
        (e.g. ``ELSN08887``) whereas ``.tsco`` files use ``EL08887``.
        This method strips the ``SN`` suffix (``ELSN`` -> ``EL``) before
        matching.  Filter-wheel positions in exposure-time files are
        1-indexed (``FW1`` -- ``FW4``) while ``.tsco`` files are
        0-indexed (``FW0`` -- ``FW3``); the method subtracts 1 from the
        exposure-time value before comparing.

        Parameters
        ----------
        path : str
            Absolute or relative path to the calibration data directory
            (e.g. ``"TEL-8050 Calibration Data/"``).

        Raises
        ------
        FileNotFoundError
            If *path* does not exist or is not a directory.

        Examples
        --------
        Load info before connecting (stored and matched on connection):

        >>> cam.load_calibration_info("/media/usb/TEL-8050 Calibration Data")
        >>> cam.connect("192.168.100.10")
        >>> cam.calibration_load(lens="50mm", temp=25)

        Load info after connecting (indices resolved immediately):

        >>> cam.connect("192.168.100.10")
        >>> cam.load_calibration_info("/media/usb/TEL-8050 Calibration Data")
        >>> cols = cam.calibration_collections()
        >>> for c in cols:
        ...     print(c["index"], c.get("lens"), c.get("temp_range"))
        """
        path = os.path.normpath(path)
        if not os.path.isdir(path):
            raise FileNotFoundError(f"Calibration directory not found: {path}")

        # --- Step 1: Parse .tsco filenames to build POSIX -> file info map ---
        # Two filename formats:
        #   Old: TEL08050_TIMESTAMP_ELXXXXX_MFXXXXX_FWn_IMn_SWDn.tsco
        #   New: TEL08050_ELXXXXX_MFXXXXX_FWn_IMn_SWDn_TIMESTAMP.tsco
        tsco_by_posix: dict[int, dict] = {}
        tsco_by_key: dict[str, dict] = {}

        for fname in os.listdir(path):
            if not fname.lower().endswith(".tsco"):
                continue

            parts = fname[:-5].split("_")  # strip .tsco, split on _
            if len(parts) < 6:
                continue

            # Detect format by checking if parts[1] is a pure digit timestamp
            if parts[1].isdigit() and len(parts[1]) >= 9:
                # Old format: TEL08050_TIMESTAMP_EL_MF_FW_IM_SWD
                posix_ts = int(parts[1])
                remaining = parts[2:]
            elif parts[-1].isdigit() and len(parts[-1]) >= 9:
                # New format: TEL08050_EL_MF_FW_IM_SWD_TIMESTAMP
                posix_ts = int(parts[-1])
                remaining = parts[1:-1]
            else:
                continue

            # Extract EL, MF, FW from remaining parts
            info = {"posix": posix_ts, "filename": fname}
            for p in remaining:
                if p.upper().startswith("EL"):
                    info["el"] = p
                elif p.upper().startswith("MF"):
                    info["mf"] = p
                elif p.upper().startswith("FW"):
                    info["fw"] = p
                    with suppress(ValueError):
                        info["fw_pos"] = int(p[2:])
                elif p.upper().startswith("IM"):
                    info["im"] = p
                elif p.upper().startswith("SWD"):
                    info["swd"] = p

            tsco_by_posix[posix_ts] = info

            # Build a lookup key from EL + FW for matching with exposure files
            el = info.get("el", "")
            fw = info.get("fw", "")
            key = f"{el}_{fw}".upper()
            tsco_by_key.setdefault(key, []).append(info)

        # --- Step 2: Parse estimated_ExposureTimes/*.txt for lens + temp ---
        et_dir = os.path.join(path, "estimated_ExposureTimes")
        lens_info: dict[str, dict] = {}  # key -> {lens_name, fw_pos, temp_min, temp_max}

        if os.path.isdir(et_dir):
            for fname in os.listdir(et_dir):
                if not fname.endswith(".txt"):
                    continue
                fpath = os.path.join(et_dir, fname)
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    header = f.readline()

                    # Extract lens name: lens "MW 50mm"
                    m = re.search(r'lens "([^"]+)"', header)
                    lens_name = m.group(1) if m else None

                    # Extract FW position: filter wheel position #1
                    m = re.search(r"filter wheel position #(\d+)", header)
                    fw_pos = int(m.group(1)) if m else None

                    # Get temp range from data rows (first column, semicolon-separated)
                    lines = [line for line in f if not line.startswith("%") and line.strip()]
                    temp_min = temp_max = None
                    if lines:
                        try:
                            temp_min = float(lines[0].split(";")[0])
                            temp_max = float(lines[-1].split(";")[0])
                        except (ValueError, IndexError):
                            pass

                # Extract EL/FW from exposure filename for matching
                # Exposure files use "ELSN08887" while .tsco uses "EL08887"
                eparts = fname[:-4].split("_")
                el_et = ""
                fw_et = ""
                for p in eparts:
                    pu = p.upper()
                    if pu.startswith("ELSN"):
                        el_et = "EL" + p[4:]  # normalize ELSN -> EL
                    elif pu.startswith("EL"):
                        el_et = p
                    elif pu.startswith("FW"):
                        # Exposure files are 1-indexed (FW1-FW4),
                        # .tsco files are 0-indexed (FW0-FW3)
                        try:
                            fw_num = int(p[2:]) - 1  # convert to 0-indexed
                            fw_et = f"FW{fw_num}"
                        except ValueError:
                            fw_et = p

                key = f"{el_et}_{fw_et}".upper()
                lens_info[key] = {
                    "lens": lens_name,
                    "fw_pos": fw_pos,
                    "temp_min": temp_min,
                    "temp_max": temp_max,
                }

        # --- Step 3: Merge lens info into tsco records ---
        for key, info_list in tsco_by_key.items():
            li = lens_info.get(key)
            if li is None:
                continue
            for info in info_list:
                info["lens"] = li["lens"]
                if li["temp_min"] is not None:
                    info["temp_range"] = (li["temp_min"], li["temp_max"])

        # --- Step 4: Map POSIX timestamps to camera collection indices ---
        # Read collection count and timestamps from camera if connected,
        # otherwise store the file-based info for later matching.
        if self._connected:
            n_collections = self._gvcp.read_reg(reg.REG_CAL_COLLECTION_COUNT)
            cal_info = {}
            for i in range(n_collections):
                self._gvcp.write_reg(reg.REG_CAL_COLLECTION_SELECTOR, i)
                posix_ts = self._gvcp.read_reg(reg.REG_CAL_COLLECTION_POSIX)
                entry = {"index": i, "posix": posix_ts}

                # Match by POSIX timestamp
                tsco = tsco_by_posix.get(posix_ts)
                if tsco:
                    entry["lens"] = tsco.get("lens")
                    entry["fw_pos"] = tsco.get("fw_pos")
                    entry["temp_range"] = tsco.get("temp_range")
                    entry["filename"] = tsco.get("filename")

                cal_info[i] = entry

            self._calibration_info = cal_info
        else:
            # Store raw file info; will be matched when camera connects
            self._calibration_file_info = tsco_by_posix
            self._calibration_lens_info = lens_info
            self._calibration_tsco_by_key = tsco_by_key

        logger.info(
            "Loaded calibration info: %d .tsco files, %d exposure time files from %s",
            len(tsco_by_posix),
            len(lens_info),
            path,
        )

    def calibration_collections(self) -> list[dict]:
        """List all calibration collections stored on the camera.

        Reads the collection count from ``REG_CAL_COLLECTION_COUNT``, then
        iterates over each index reading its POSIX timestamp, type, and
        block count.  Optional fields are included only when the
        corresponding data is available.

        Returns
        -------
        list of dict
            One dict per collection.  Always-present keys:

            ``"index"`` : int
                0-based collection index.
            ``"timestamp"`` : datetime.datetime
                Collection creation time (UTC, timezone-aware).
            ``"posix"`` : int
                Raw POSIX timestamp of the collection.
            ``"type"`` : str or int
                Calibration type name from
                :class:`reg.CalibrationCollectionType` (e.g.
                ``"TELOPS_FIXED"``), or the raw integer if unknown.
            ``"blocks"`` : int
                Number of calibration blocks in this collection.

            Optional keys (present when :meth:`load_calibration_info` was
            called and matched this collection):

            ``"lens"`` : str
                Human-readable lens name (e.g. ``"MW 50mm"``).
            ``"fw_position"`` : int
                0-based filter-wheel position.
            ``"temp_range"`` : tuple[float, float]
                ``(temp_min, temp_max)`` in degrees Celsius.

            Optional key (present when :attr:`calibration_names` includes
            this index):

            ``"name"`` : str
                Manually assigned human-readable name.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        import datetime

        n = self._gvcp.read_reg(reg.REG_CAL_COLLECTION_COUNT)
        collections = []

        for i in range(n):
            self._gvcp.write_reg(reg.REG_CAL_COLLECTION_SELECTOR, i)
            posix_ts = self._gvcp.read_reg(reg.REG_CAL_COLLECTION_POSIX)
            cal_type = self._gvcp.read_reg(reg.REG_CAL_COLLECTION_TYPE)
            block_count = self._gvcp.read_reg(reg.REG_CAL_BLOCK_COUNT)

            dt = datetime.datetime.fromtimestamp(posix_ts, tz=datetime.timezone.utc)

            entry = {
                "index": i,
                "timestamp": dt,
                "posix": posix_ts,
                "type": reg.CalibrationCollectionType(cal_type).name
                if cal_type in reg.CalibrationCollectionType.__members__.values()
                else cal_type,
                "blocks": block_count,
            }

            # Add info from load_calibration_info if available
            if i in self._calibration_info:
                ci = self._calibration_info[i]
                if ci.get("lens"):
                    entry["lens"] = ci["lens"]
                if ci.get("fw_pos") is not None:
                    entry["fw_position"] = ci["fw_pos"]
                if ci.get("temp_range"):
                    entry["temp_range"] = ci["temp_range"]

            # Add manual name if set
            if i in self._calibration_names:
                entry["name"] = self._calibration_names[i]

            collections.append(entry)

        return collections

    def calibration_load(
        self, index: int | None = None, lens: str | None = None, temp: float | None = None
    ) -> dict:
        """Load a calibration collection and activate its first block.

        Exactly one of ``index`` or ``lens`` must be supplied.  When
        ``lens`` is given, :meth:`load_calibration_info` must have been
        called first so that lens/temperature metadata is available.

        The method writes ``REG_CAL_COLLECTION_SELECTOR`` to select the
        target collection, then compares its POSIX timestamp to the
        currently active POSIX timestamp.  If they match the collection
        is already loaded and no further register writes are performed.
        Otherwise it writes ``REG_CAL_COLLECTION_LOAD``, waits 2 s, then
        loads block 0 via ``REG_CAL_BLOCK_LOAD`` and waits another 2 s.
        A :class:`UserWarning` is emitted if the active POSIX register
        does not match the expected value after loading (the camera may
        still be processing).

        Parameters
        ----------
        index : int or None, optional
            0-based collection index.  Use when you know the exact index.
        lens : str or None, optional
            Lens name substring to search (case-insensitive, e.g.
            ``"50mm"``, ``"microscope"``).  When multiple collections
            match the lens name, the one whose ``temp_range`` contains
            *temp* is preferred; ties are broken by selecting the
            narrowest temperature range.
        temp : float or None, optional
            Target scene temperature in degrees Celsius.  Used together
            with *lens* to select the collection whose calibrated
            temperature range covers this value.  Ignored when *index*
            is specified.

        Returns
        -------
        dict
            Information about the loaded collection.  Always-present keys:

            ``"index"`` : int
                Collection index that was loaded.
            ``"posix"`` : int
                POSIX timestamp of the loaded collection.

            Optional keys (present when metadata was available from
            :meth:`load_calibration_info` or :attr:`calibration_names`):

            ``"lens"`` : str, ``"fw_position"`` : int,
            ``"temp_range"`` : tuple[float, float], ``"name"`` : str.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        ValueError
            If neither *index* nor *lens* is provided, or if no collection
            matches the *lens* / *temp* criteria.

        Examples
        --------
        Load by explicit index:

        >>> cam.calibration_load(index=4)

        Load by lens and target temperature (requires prior
        :meth:`load_calibration_info` call):

        >>> cam.load_calibration_info("/media/usb/TEL-8050 Calibration Data")
        >>> cam.calibration_load(lens="50mm", temp=25)

        >>> cam.calibration_load(lens="microscope", temp=300)
        """
        self._check_connected()

        if index is not None:
            pass  # use directly
        elif lens is not None:
            if not self._calibration_info:
                raise ValueError(
                    "No calibration info loaded. Call load_calibration_info() "
                    "first, or use index= to specify by collection index."
                )

            # Search for matching lens + temp range
            candidates = []
            lens_lower = lens.lower()
            for idx, ci in self._calibration_info.items():
                ci_lens = ci.get("lens")
                if ci_lens is None:
                    continue
                if lens_lower not in ci_lens.lower():
                    continue

                if temp is not None and ci.get("temp_range"):
                    t_min, t_max = ci["temp_range"]
                    if t_min <= temp <= t_max:
                        candidates.append((idx, ci, t_max - t_min))
                elif temp is None:
                    candidates.append((idx, ci, float("inf")))

            if not candidates:
                # Build helpful error message
                available = []
                for idx, ci in self._calibration_info.items():
                    ci_lens = ci.get("lens", "unknown")
                    tr = ci.get("temp_range")
                    tr_str = f" ({tr[0]:.0f}-{tr[1]:.0f} C)" if tr else ""
                    available.append(f"  [{idx}] {ci_lens}{tr_str}")
                avail_str = "\n".join(available) if available else "  (none)"
                raise ValueError(
                    f"No calibration collection matches lens={lens!r}, "
                    f"temp={temp}.\nAvailable:\n{avail_str}"
                )

            # Prefer narrowest temperature range
            candidates.sort(key=lambda x: x[2])
            index = candidates[0][0]
        else:
            raise ValueError("Specify index= or lens= (with optional temp=)")

        # --- Load the collection (skip if already active) ---
        self._gvcp.write_reg(reg.REG_CAL_COLLECTION_SELECTOR, index)
        target_posix = self._gvcp.read_reg(reg.REG_CAL_COLLECTION_POSIX)
        active_posix = self._gvcp.read_reg(reg.REG_CAL_ACTIVE_POSIX)

        if target_posix == active_posix:
            # Already loaded — skip
            pass
        else:
            self._gvcp.write_reg(reg.REG_CAL_COLLECTION_LOAD, 1)
            time.sleep(2.0)

            self._gvcp.write_reg(reg.REG_CAL_BLOCK_SELECTOR, 0)
            self._gvcp.write_reg(reg.REG_CAL_BLOCK_LOAD, 1)
            time.sleep(2.0)

        # Verify active POSIX matches what we selected
        self._gvcp.write_reg(reg.REG_CAL_COLLECTION_SELECTOR, index)
        expected_posix = self._gvcp.read_reg(reg.REG_CAL_COLLECTION_POSIX)
        active_posix = self._gvcp.read_reg(reg.REG_CAL_ACTIVE_POSIX)
        if active_posix != expected_posix:
            import warnings

            warnings.warn(
                f"Calibration load verification: active POSIX {active_posix} "
                f"!= expected {expected_posix}. The camera may still be "
                f"loading.",
                UserWarning,
                stacklevel=2,
            )

        # Build result info
        result = {"index": index, "posix": expected_posix}

        # Add details from calibration info
        ci = self._calibration_info.get(index, {})
        lens_name = ci.get("lens")
        fw_pos = ci.get("fw_pos")
        temp_range = ci.get("temp_range")

        if lens_name:
            result["lens"] = lens_name
        if fw_pos is not None:
            result["fw_position"] = fw_pos
        if temp_range:
            result["temp_range"] = temp_range

        # Add manual name if set
        if index in self._calibration_names:
            result["name"] = self._calibration_names[index]

        # Print summary
        desc_parts = []
        if lens_name:
            desc_parts.append(lens_name)
        elif index in self._calibration_names:
            desc_parts.append(self._calibration_names[index])
        else:
            desc_parts.append(f"Collection {index}")
        if fw_pos is not None:
            desc_parts.append(f"FW{fw_pos}")
        if temp_range:
            desc_parts.append(f"({temp_range[0]:.0f}-{temp_range[1]:.0f} C)")

        logger.info("Loaded: %s", " ".join(desc_parts))

        return result

    def calibration_active(self) -> dict:
        """Return information about the currently active calibration.

        Reads the active calibration type, collection POSIX timestamp, and
        block POSIX timestamp from camera registers.  If calibration
        metadata was loaded via :meth:`load_calibration_info`, the result
        is enriched with lens, filter-wheel position, and temperature range.
        If :attr:`calibration_names` contains an entry for the active
        collection index, its name is included as well.

        Returns
        -------
        dict
            Always-present keys:

            ``"type"`` : str or int
                Calibration type name from
                :class:`reg.CalibrationCollectionType` (e.g.
                ``"TELOPS_FIXED"``), or the raw integer if unknown.
            ``"collection_posix"`` : int
                POSIX timestamp of the active collection (``0`` if none).
            ``"collection_timestamp"`` : datetime.datetime or None
                UTC datetime for the active collection, or ``None`` when
                the POSIX value is zero.
            ``"block_posix"`` : int
                POSIX timestamp of the active block (``0`` if none).
            ``"block_timestamp"`` : datetime.datetime or None
                UTC datetime for the active block, or ``None`` when the
                POSIX value is zero.

            Optional keys (present when metadata is available):

            ``"index"`` : int
                Matched collection index.
            ``"lens"`` : str
                Lens name from :meth:`load_calibration_info`.
            ``"fw_position"`` : int
                0-based filter-wheel position.
            ``"temp_range"`` : tuple[float, float]
                ``(temp_min, temp_max)`` in degrees Celsius.
            ``"name"`` : str
                Manually assigned name from :attr:`calibration_names`.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        import datetime

        cal_type = self._gvcp.read_reg(reg.REG_CAL_ACTIVE_TYPE)
        col_posix = self._gvcp.read_reg(reg.REG_CAL_ACTIVE_POSIX)
        blk_posix = self._gvcp.read_reg(reg.REG_CAL_ACTIVE_BLOCK_POSIX)

        col_dt = (
            datetime.datetime.fromtimestamp(col_posix, tz=datetime.timezone.utc)
            if col_posix
            else None
        )
        blk_dt = (
            datetime.datetime.fromtimestamp(blk_posix, tz=datetime.timezone.utc)
            if blk_posix
            else None
        )

        result = {
            "type": reg.CalibrationCollectionType(cal_type).name
            if cal_type in reg.CalibrationCollectionType.__members__.values()
            else cal_type,
            "collection_posix": col_posix,
            "collection_timestamp": col_dt,
            "block_posix": blk_posix,
            "block_timestamp": blk_dt,
        }

        # Match active collection to calibration info
        if self._calibration_info:
            active_posix = result.get("collection_posix")
            for idx, info in self._calibration_info.items():
                if info.get("posix") == active_posix:
                    result["index"] = idx
                    result["lens"] = info.get("lens")
                    result["fw_position"] = info.get("fw_pos")
                    result["temp_range"] = info.get("temp_range")
                    break

        # Manual names
        if self._calibration_names:
            idx = result.get("index")
            if idx is not None and idx in self._calibration_names:
                result["name"] = self._calibration_names[idx]

        return result

    # ==========================================================
    # Memory Buffer (16GB onboard)
    # ==========================================================

    def buffer_configure(
        self,
        n_sequences: int = 1,
        duration: float | None = None,
        frames_per_seq: int | None = None,
        pre_moi: int = 0,
        moi_source: str | reg.MemoryBufferMOISource = "software",
    ) -> None:
        """Configure the internal memory buffer for recording.

        The camera has a 16 GB ring buffer that records at full sensor speed
        (up to 3100 fps at full frame), independent of the Ethernet link.
        The buffer is partitioned into fixed-size sequence slots; each slot
        holds exactly *frames_per_seq* frames. The MOI (Moment of Interest)
        trigger marks the transition from pre-trigger to post-trigger frames
        within each slot.

        Specify either *duration* (seconds; the current frame rate is used to
        calculate the frame count) or *frames_per_seq* (exact frame count).
        If neither is given, 100 frames per sequence is used.

        If the buffer is already active or in an incompatible state, the
        method automatically stops acquisition, clears the buffer, and
        re-enables buffer mode before writing the new parameters.

        Parameters
        ----------
        n_sequences : int, optional
            Number of sequence slots to allocate in the buffer.  Default 1.
        duration : float or None, optional
            Recording duration per sequence in seconds.  The frame count is
            derived from the current :attr:`frame_rate`.  Mutually exclusive
            with *frames_per_seq*.
        frames_per_seq : int or None, optional
            Exact number of frames per sequence slot.  Mutually exclusive
            with *duration*.  Defaults to 100 when neither argument is given.
        pre_moi : int, optional
            Number of frames to preserve before the MOI trigger event.
            These frames are the "pre-trigger" portion of each slot.
            Default 0 (all frames are post-MOI).
        moi_source : str or reg.MemoryBufferMOISource, optional
            Source of the MOI trigger.  Accepted strings (case-insensitive):
            ``"software"`` -- fire via :meth:`buffer_fire_moi` (default),
            ``"external"`` -- hardware signal on the BNC connector,
            ``"acquisition_started"`` -- MOI fires automatically when
            acquisition begins,
            ``"none"`` -- no MOI (record-until-full).  Also accepts the
            :class:`reg.MemoryBufferMOISource` enum or its integer value.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        ValueError
            If both *duration* and *frames_per_seq* are specified, or if
            *duration* results in zero frames at the current frame rate.

        Examples
        --------
        Record 200 frames split into two sequences with a software MOI:

        >>> cam.buffer_configure(n_sequences=2, frames_per_seq=100)

        Record 0.5 s at current frame rate with 50 pre-trigger frames:

        >>> cam.buffer_configure(duration=0.5, pre_moi=50)

        Configure for external hardware trigger:

        >>> cam.buffer_configure(frames_per_seq=500, moi_source="external")
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
                    f"frames. Set frame_rate first."
                )
        elif frames_per_seq is None:
            frames_per_seq = 100

        # Track configured sequence count for buffer_record()
        self._buffer_n_sequences = n_sequences

        # Try to enable buffer mode; if it fails, clean up stale state
        try:
            self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_MODE, reg.MemoryBufferMode.ON)
        except GVCPError:
            with suppress(GVCPError):
                self._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)
            time.sleep(0.3)
            with suppress(GVCPError):
                self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_CLEAR_ALL, 1)
            time.sleep(0.3)
            with suppress(GVCPError):
                self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_MODE, reg.MemoryBufferMode.OFF)
            time.sleep(0.3)
            self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_MODE, reg.MemoryBufferMode.ON)

        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_NUM_SEQUENCES, n_sequences)
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_SEQ_SIZE, frames_per_seq)
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_PRE_MOI_SIZE, pre_moi)
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_MOI_SOURCE, int(moi))

        # Remember the exact parameters so buffer_clear() can re-apply
        # them automatically (the camera wipes partition state on clear).
        self._buffer_config_kwargs = dict(
            n_sequences=n_sequences,
            frames_per_seq=frames_per_seq,
            pre_moi=pre_moi,
            moi_source=moi_source,
        )

    def buffer_record(self, verbose: bool = True) -> int:
        """Record all configured sequences to the internal buffer.

        Arms the camera on the first sequence and fires a software MOI for
        each sequence in turn.  After firing the MOI, the method polls
        ``REG_MEMORY_BUFFER_SEQ_COUNT`` (0xE914) to detect per-sequence
        completion rather than waiting for the overall buffer status to
        leave RECORDING.  Acquisition is stopped after the final sequence.

        The per-sequence timeout is derived automatically from the configured
        frame count and current frame rate (at least 30 s overhead, minimum
        45 s total).

        For external-trigger workflows where the MOI comes from a hardware
        signal, use the manual flow instead::

            cam.buffer_configure(n_sequences=3, moi_source="external")
            cam.buffer_arm()
            # ... external trigger fires 3 times ...
            cam.buffer_wait()       # waits for HOLDING/IDLE

        Parameters
        ----------
        verbose : bool, optional
            Print per-sequence progress messages to stdout.  Default ``True``.

        Returns
        -------
        int
            Total number of frames recorded across all sequences, read
            from the camera registers after acquisition stops.

        Raises
        ------
        RuntimeError
            If the camera is not connected or not ready.
        TimeoutError
            If a sequence does not complete within the computed safety
            timeout.

        Examples
        --------
        Record a single sequence:

        >>> cam.buffer_configure(n_sequences=1, frames_per_seq=100)
        >>> n = cam.buffer_record()
        >>> print(n)  # 100

        Record three sequences back-to-back:

        >>> cam.buffer_configure(n_sequences=3, frames_per_seq=50)
        >>> n = cam.buffer_record()
        >>> print(n)  # 150
        """
        self._check_ready()
        n_seq = getattr(self, "_buffer_n_sequences", 1)

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
                    print(f"Arming (seq {seq_idx + 1}/{n_seq})...", end=" ", flush=True)

                self._gvcp.write_reg(reg.REG_ACQUISITION_ARM, 1)
                self._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)
                time.sleep(0.5)
            else:
                # Subsequent sequences: camera stays armed, just fire MOI
                if verbose:
                    print(f"Firing (seq {seq_idx + 1}/{n_seq})...", end=" ", flush=True)

            if verbose:
                print("Recording...", end=" ", flush=True)

            self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_MOI_SOFTWARE, 1)

            # Wait for this sequence to complete
            try:
                self._buffer_wait_sequence(seq_idx + 1, timeout=timeout)
            except TimeoutError:
                # On timeout of the last sequence, stop acquisition
                with suppress(GVCPError):
                    self._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)
                if verbose:
                    print("TIMEOUT", flush=True)
                raise

            if verbose:
                print(f"Done ({seq_size} frames)", flush=True)
            total_recorded += seq_size

        # Stop acquisition after all sequences complete
        with suppress(GVCPError):
            self._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)
        time.sleep(0.3)

        # Now read actual per-sequence counts (registers unlocked after stop)
        total_recorded = 0
        for i in range(n_seq):
            total_recorded += self.buffer_recorded_frames(i)

        return total_recorded

    def buffer_arm(self) -> None:
        """Arm the camera and start acquisition for buffer recording.

        Writes ``REG_ACQUISITION_ARM`` then ``REG_ACQUISITION_START``.
        Use this as the first step of the manual external-trigger workflow::

            cam.buffer_configure(n_sequences=1, moi_source="external")
            cam.buffer_arm()
            # ... external MOI signal fires ...
            cam.buffer_wait()

        To fire the MOI in software instead, call :meth:`buffer_fire_moi`
        after arming.  For fully automated software-MOI recordings, prefer
        :meth:`buffer_record`.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        self._gvcp.write_reg(reg.REG_ACQUISITION_ARM, 1)
        self._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)

    def buffer_fire_moi(self) -> None:
        """Fire a software MOI (Moment of Interest) trigger.

        Writes ``1`` to ``REG_MEMORY_BUFFER_MOI_SOFTWARE``.  Only has effect
        when :meth:`buffer_arm` has been called and
        ``moi_source`` was set to ``"software"`` in :meth:`buffer_configure`.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_MOI_SOFTWARE, 1)

    def buffer_wait(
        self, timeout: float = 30.0, poll_interval: float = 0.5
    ) -> reg.MemoryBufferStatus:
        """Wait for buffer recording to complete.

        Polls :meth:`buffer_status` at *poll_interval* second intervals
        until the status is ``HOLDING`` or ``IDLE``, indicating that all
        configured sequences have finished recording.

        Parameters
        ----------
        timeout : float, optional
            Maximum number of seconds to wait.  Default 30.0.
        poll_interval : float, optional
            Seconds between consecutive status reads.  Default 0.5.

        Returns
        -------
        reg.MemoryBufferStatus
            The final buffer status (``HOLDING`` or ``IDLE``) when
            recording completes.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        TimeoutError
            If the buffer does not reach ``HOLDING`` or ``IDLE`` within
            *timeout* seconds.
        """
        self._check_connected()
        status = self.buffer_status()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.buffer_status()
            if status in (reg.MemoryBufferStatus.HOLDING, reg.MemoryBufferStatus.IDLE):
                return status
            time.sleep(poll_interval)
        raise TimeoutError(
            f"Buffer recording not complete after {timeout:.0f}s (last status: {status.name})"
        )

    def _buffer_wait_sequence(
        self, target_count: int, timeout: float = 30.0, poll_interval: float = 0.5
    ) -> None:
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
            f"{self._gvcp.read_reg(reg.REG_MEMORY_BUFFER_SEQ_COUNT)})"
        )

    def buffer_info(self) -> dict:
        """Return a summary of buffer state and recorded sequences.

        Reads the buffer status register, iterates over configured sequence
        slots to collect per-sequence frame counts, and reads the 64-bit
        total/free space registers (split across two 32-bit hi/lo registers).

        Returns
        -------
        dict
            A dict with the following keys:

            ``"status"`` : str
                Name of the current :class:`reg.MemoryBufferStatus` value
                (e.g. ``"IDLE"``, ``"HOLDING"``, ``"RECORDING"``).
            ``"n_sequences"`` : int
                Number of sequence slots configured via
                :meth:`buffer_configure`.
            ``"recorded"`` : list[int]
                Frame count for each sequence slot (0-based index).
                Entries may be 0 if a slot has not been used or if the
                camera returns a :class:`GVCPError` for that slot.
            ``"total_bytes"`` : int
                Total capacity of the onboard buffer in bytes.
            ``"free_bytes"`` : int
                Remaining free space in the buffer in bytes.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        status = self.buffer_status()
        n_seq = getattr(self, "_buffer_n_sequences", 1)

        recorded = []
        for i in range(n_seq):
            try:
                self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_SEQ_SELECTOR, i)
                count = self._gvcp.read_reg(reg.REG_MEMORY_BUFFER_SEQ_RECORDED_SIZE)
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
        """Read the current memory buffer status.

        Writes the ``REFRESH`` sentinel to ``REG_MEMORY_BUFFER_STATUS`` to
        force the camera to update the register, then reads and returns the
        result.  The refresh write is silently ignored on cameras that do not
        support it.

        Returns
        -------
        reg.MemoryBufferStatus
            Current buffer status.  Possible values:
            ``DEACTIVATED``, ``IDLE``, ``HOLDING``, ``RECORDING``,
            ``UPDATING``, ``TRANSMITTING``, ``DEFRAGGING``.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        with suppress(GVCPError):
            self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_STATUS, reg.MemoryBufferStatus.REFRESH)
        val = self._gvcp.read_reg(reg.REG_MEMORY_BUFFER_STATUS)
        return reg.MemoryBufferStatus(val)

    def buffer_recorded_frames(self, sequence: int = 0) -> int:
        """Return the number of frames recorded in a sequence slot.

        Writes the sequence selector register then reads
        ``REG_MEMORY_BUFFER_SEQ_RECORDED_SIZE``.

        Parameters
        ----------
        sequence : int, optional
            0-based sequence slot index.  Default 0.

        Returns
        -------
        int
            Number of frames recorded in the selected sequence slot.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        GVCPError
            If the camera rejects the register access, for example while
            the buffer is actively recording.
        """
        self._check_connected()
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_SEQ_SELECTOR, sequence)
        return self._gvcp.read_reg(reg.REG_MEMORY_BUFFER_SEQ_RECORDED_SIZE)

    def buffer_download(
        self,
        sequence: int = 0,
        start_frame: int | None = None,
        n_frames: int = 0,
        timeout: float = 0,
        bitrate_mbps: float = 1000.0,
        packet_size: int = 1500,
        strip_header: bool = True,
        convert: bool = True,
        verbose: bool = True,
    ) -> np.ndarray | None:
        """Download frames from the internal memory buffer over Ethernet.

        Sets the download mode to ``SEQUENCE``, configures frame range and
        packet size, starts an acquisition stream, and collects frames via
        :class:`pyGigEVision.GVSPReceiver`.  The bitrate cap register is
        temporarily raised to *bitrate_mbps* to saturate the link, then
        restored on exit.  Packet size and DoNotFragment flags are also
        restored after the transfer.

        When *convert* is ``True`` and calibration mode is ``"RT"``, each
        frame is converted to degrees Celsius using the per-frame header's
        ``DataExp`` and ``DataOffset`` fields (same path as :meth:`grab`).

        *bitrate_mbps* can be lowered (e.g. to ``300``) to reduce host
        network contention on machines running video-conferencing software
        (Teams, Zoom) during long transfers.

        Parameters
        ----------
        sequence : int, optional
            0-based sequence slot index to download.  Default 0.
        start_frame : int or None, optional
            First frame ID to request.  ``None`` (default) starts from the
            first recorded frame in the slot.
        n_frames : int, optional
            Number of frames to download.  ``0`` (default) downloads all
            recorded frames in the slot.
        timeout : float, optional
            Total download timeout in seconds.  ``0`` (default) uses an
            auto-calculated value based on frame count (1.5x the expected
            transfer time, minimum 10 s).
        bitrate_mbps : float, optional
            Maximum download bitrate in Mbit/s written to the camera's
            ``REG_DOWNLOAD_BITRATE_MAX`` register.  Default 1000.0.
            Reduce to around 300 when network contention is a concern.
        packet_size : int, optional
            GVSP UDP payload size in bytes.  Default 1500 (standard
            Ethernet MTU).  Use 9000 on a jumbo-frame network for faster
            downloads; the driver clears the DoNotFragment flag
            automatically when *packet_size* > 1500.
        strip_header : bool, optional
            Strip the two Telops metadata rows from each frame.  Default
            ``True``.  Ignored when *convert* is ``True`` (stripping is
            implicit in the calibration path).
        convert : bool, optional
            Apply calibration (strip headers and convert to Celsius in RT
            mode).  Default ``True``.
        verbose : bool, optional
            Show a ``tqdm`` progress bar and log a transfer summary.
            Default ``True``.

        Returns
        -------
        numpy.ndarray or None
            Array of shape ``(N, H, W)`` where ``N`` is the number of
            frames received, ``H`` and ``W`` are the usable pixel dimensions.
            ``dtype`` is ``float32`` when *convert* is ``True`` and RT mode
            is active; otherwise ``uint16``.  Returns ``None`` when no
            frames were recorded in the slot or no frames were received
            within the timeout.

        Raises
        ------
        RuntimeError
            If the camera is not connected.

        Examples
        --------
        Download all frames from sequence 0 (default):

        >>> frames = cam.buffer_download()
        >>> frames.shape
        (100, 254, 320)

        Download a specific sequence with jumbo frames:

        >>> frames = cam.buffer_download(sequence=1, packet_size=9000)

        Throttle transfer rate to reduce network contention:

        >>> frames = cam.buffer_download(bitrate_mbps=300.0)
        """
        self._check_connected()

        # Select sequence and get info
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_SEQ_SELECTOR, sequence)

        if n_frames == 0:
            n_frames = self._gvcp.read_reg(reg.REG_MEMORY_BUFFER_SEQ_RECORDED_SIZE)

        if n_frames == 0:
            if verbose:
                logger.warning("No frames recorded in buffer")
            return None

        first_frame_id = self._gvcp.read_reg(reg.REG_MEMORY_BUFFER_SEQ_FIRST_FRAME_ID)
        if start_frame is None:
            start_frame = first_frame_id

        if timeout <= 0:
            timeout = max(n_frames / 200.0 * 1.5 + 5.0, 10.0)

        # Set up progress bar
        pbar = None
        if verbose:
            from tqdm import tqdm

            pbar = tqdm(total=n_frames, unit="frame", desc="Downloading")

        # Suppress GVSP "packets unrecoverable" warnings during download
        import logging

        gvsp_logger = logging.getLogger("pyGigEVision.gvsp")
        old_level = gvsp_logger.level
        gvsp_logger.setLevel(logging.CRITICAL)

        # Ensure acquisition is stopped before configuring download
        with suppress(GVCPError):
            self._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)
        time.sleep(0.2)

        # Configure download — mode MUST be set before other registers
        # (they are locked when mode == OFF)
        self._gvcp.write_reg(
            reg.REG_MEMORY_BUFFER_DOWNLOAD_MODE, reg.MemoryBufferDownloadMode.SEQUENCE
        )

        # Increase download bitrate (register unlocked now that mode != OFF)
        old_bitrate = None
        try:
            old_bitrate = self._gvcp.read_float(reg.REG_DOWNLOAD_BITRATE_MAX)
            if bitrate_mbps != old_bitrate:
                self._gvcp.write_float(reg.REG_DOWNLOAD_BITRATE_MAX, bitrate_mbps)
        except GVCPError:
            pass

        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_DOWNLOAD_FRAME_ID, start_frame)
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_DOWNLOAD_FRAME_COUNT, n_frames)

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
            old_pkt_reg = self._gvcp.read_reg(REG_SC_PACKET_SIZE)
            upper_flags = old_pkt_reg & 0xFFFF0000
            new_pkt_reg = upper_flags | (packet_size & SC_PACKET_SIZE_MASK)
            if packet_size > 1500:
                # Allow IP fragmentation for large packets
                new_pkt_reg &= ~SC_SCPS_DO_NOT_FRAGMENT
            self._gvcp.write_reg(REG_SC_PACKET_SIZE, new_pkt_reg)
            self._gvsp._packet_data_size = packet_size - 8

        # Start download stream
        self._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)

        # Collect frames
        frames = []
        t_start = time.monotonic()
        deadline = time.monotonic() + timeout

        try:
            stall_deadline = time.monotonic() + 10.0
            for _ in range(n_frames):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                result = self._gvsp.get_frame(timeout=min(remaining, 5.0))
                if result is not None:
                    frames.append(result)
                    stall_deadline = time.monotonic() + 10.0
                    if pbar:
                        pbar.update(1)
                elif time.monotonic() > stall_deadline:
                    break  # no frames for 10s — stream is dead
        finally:
            if pbar:
                pbar.close()
            with suppress(GVCPError):
                self._gvcp.write_reg(reg.REG_ACQUISITION_STOP, 1)
            time.sleep(0.2)
            if old_bitrate is not None:
                with suppress(GVCPError):
                    self._gvcp.write_float(reg.REG_DOWNLOAD_BITRATE_MAX, old_bitrate)
            with suppress(GVCPError):
                self._gvcp.write_reg(
                    reg.REG_MEMORY_BUFFER_DOWNLOAD_MODE, reg.MemoryBufferDownloadMode.OFF
                )
            if old_pkt_reg is not None:
                with suppress(GVCPError):
                    # Restore original register value (size + flags incl.
                    # DoNotFragment) exactly as it was before download.
                    self._gvcp.write_reg(REG_SC_PACKET_SIZE, old_pkt_reg)
                    self._gvsp._packet_data_size = (old_pkt_reg & SC_PACKET_SIZE_MASK) - 8
            self._gvsp.resend_enabled = True
            self.stop_stream()
            gvsp_logger.setLevel(old_level)

        elapsed = time.monotonic() - t_start
        if verbose and frames:
            fps = len(frames) / elapsed if elapsed > 0 else 0
            mbps = (
                len(frames) * self._gvcp.read_reg(reg.REG_PAYLOAD_SIZE) / elapsed / 1e6
                if elapsed > 0
                else 0
            )
            logger.info(
                "Downloaded %d frames in %.1fs (%d fps, %.1f MB/s)", len(frames), elapsed, fps, mbps
            )

        if not frames:
            return None
        result = np.stack(frames)
        if convert:
            result = self._apply_calibration(result)
        elif strip_header:
            result = self._strip_headers(result)

        if verbose:
            self._download_diagnostics(result, n_frames)

        return result

    @staticmethod
    def _download_diagnostics(data: np.ndarray, expected: int) -> None:
        """Print data integrity summary after download."""
        n = data.shape[0]
        frame_means = data.mean(axis=tuple(range(1, data.ndim)))
        # For calibrated (float32) data, 0.0 is a valid temperature — skip blank-frame check.
        zero_frames = 0 if data.dtype == np.float32 else int(np.sum(frame_means == 0))
        row_sums = data.reshape(n, data.shape[1], -1).sum(axis=2)
        # Same rationale for zero-row check.
        frames_with_zero_rows = (
            0 if data.dtype == np.float32 else int(np.sum(np.any(row_sums == 0, axis=1)))
        )

        issues = []
        if n < expected:
            issues.append(f"{expected - n} frames missing")
        if zero_frames > 0:
            issues.append(f"{zero_frames} blank frames")
        if frames_with_zero_rows > 0:
            issues.append(f"{frames_with_zero_rows} frames with zero rows")

        if issues:
            logger.warning("Data check: %s", ", ".join(issues))
        else:
            logger.info(
                "Data check: OK — %d frames, range [%s–%s], mean %.0f",
                n,
                data.min(),
                data.max(),
                data.mean(),
            )

    def buffer_clear(self) -> None:
        """Clear all recorded sequences from the memory buffer.

        Writes ``1`` to ``REG_MEMORY_BUFFER_CLEAR_ALL``.  The camera wipes
        both the recorded frame data and the partition configuration
        (sequence count, sequence size, pre-MOI count, MOI source) as a
        side effect of the clear.

        To keep the natural ``clear -> record -> download`` cycle working,
        this method automatically re-applies the last-used
        :meth:`buffer_configure` parameters (with a short settle delay)
        after clearing.  If :meth:`buffer_configure` has not been called in
        this session, only the clear register write is performed.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        """
        self._check_connected()
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_CLEAR_ALL, 1)

        # Re-apply the last-used partition configuration so a subsequent
        # buffer_record() has a valid partition to record into.
        if self._buffer_config_kwargs is not None:
            # Small settle delay after the clear before re-configuring
            time.sleep(0.1)
            self.buffer_configure(**self._buffer_config_kwargs)

    # ==========================================================
    # GUI
    # ==========================================================

    def live_view(self, colormap: str = "inferno", scale: int = 2) -> None:
        """Open a live thermal image viewer window.

        Launches the :class:`pyTelops.gui.LiveView` Tk-based viewer, which
        grabs frames from the camera and displays them in real time using the
        specified colormap.  The call blocks until the viewer window is
        closed.

        Requires the ``gui`` optional dependency group::

            pip install pyTelops[gui]

        Parameters
        ----------
        colormap : str, optional
            Matplotlib colormap name applied to the thermal image.
            Default ``"inferno"``.
        scale : int, optional
            Integer upscale factor applied to each dimension of the displayed
            image.  ``2`` (default) doubles the width and height.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        ImportError
            If the ``gui`` extra is not installed.
        """
        from .gui import LiveView

        viewer = LiveView(self, colormap=colormap, scale=scale)
        viewer.run()

    # ==========================================================
    # Low-level Register Access
    # ==========================================================

    def read_register(self, addr: int) -> int:
        """Read a raw 32-bit integer register value.

        Low-level escape hatch that issues a GVCP ``ReadReg`` command
        directly for a given register address.  Prefer the typed properties
        (e.g. :attr:`frame_rate`, :attr:`integration_time`) for routine use.

        Parameters
        ----------
        addr : int
            32-bit register address (byte offset from the GVCP bootstrap
            base, as defined in :mod:`pyTelops.registers`).

        Returns
        -------
        int
            The 32-bit unsigned integer value read from the register.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        GVCPError
            If the camera returns an error response for the address.
        """
        self._check_connected()
        return self._gvcp.read_reg(addr)

    def write_register(self, addr: int, value: int) -> None:
        """Write a raw 32-bit integer value to a register.

        Low-level escape hatch that issues a GVCP ``WriteReg`` command
        directly.  Prefer the typed properties for routine use.

        Parameters
        ----------
        addr : int
            32-bit register address (byte offset from the GVCP bootstrap
            base, as defined in :mod:`pyTelops.registers`).
        value : int
            32-bit unsigned integer value to write.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        GVCPError
            If the camera returns an error response for the address.
        """
        self._check_connected()
        self._gvcp.write_reg(addr, value)

    def read_float_register(self, addr: int) -> float:
        """Read a register and interpret its bits as an IEEE 754 float.

        Uses :meth:`pyGigEVision.GVCPClient.read_float` to reinterpret
        the raw 32-bit register value as a single-precision float via
        ``struct.unpack``.  Use for camera registers that store floating-
        point values (e.g. frame rate, temperature setpoints).

        Parameters
        ----------
        addr : int
            32-bit register address (byte offset from the GVCP bootstrap
            base, as defined in :mod:`pyTelops.registers`).

        Returns
        -------
        float
            The register value interpreted as an IEEE 754 single-precision
            float.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        GVCPError
            If the camera returns an error response for the address.
        """
        self._check_connected()
        return self._gvcp.read_float(addr)

    def write_float_register(self, addr: int, value: float) -> None:
        """Write a float value to a register as IEEE 754 bits.

        Uses :meth:`pyGigEVision.GVCPClient.write_float` to pack *value*
        as a single-precision float via ``struct.pack`` before writing.

        Parameters
        ----------
        addr : int
            32-bit register address (byte offset from the GVCP bootstrap
            base, as defined in :mod:`pyTelops.registers`).
        value : float
            Value to write, encoded as an IEEE 754 single-precision float.

        Raises
        ------
        RuntimeError
            If the camera is not connected.
        GVCPError
            If the camera returns an error response for the address.
        """
        self._check_connected()
        self._gvcp.write_float(addr, value)
