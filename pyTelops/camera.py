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

    def __repr__(self) -> str:
        status = "connected" if self._connected else "disconnected"
        ip = self._camera_ip or "unknown"
        return f"Camera({ip}, {status})"

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

        # Prepare GVSP receiver
        self._gvsp = GVSPReceiver(self._local_ip, gvcp_client=self._gvcp)

        self._connected = True

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
    def exposure_auto(self, mode: reg.ExposureAuto):
        self._check_connected()
        self._gvcp.write_reg(reg.REG_EXPOSURE_AUTO, int(mode))

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
    def calibration_mode(self, mode: reg.CalibrationMode):
        self._check_connected()
        self._gvcp.write_reg(reg.REG_CALIBRATION_MODE, int(mode))

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

    def grab(self, timeout: float = 5.0) -> Optional[np.ndarray]:
        """Grab a single frame.

        Starts streaming if not already active, grabs one frame.

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

        return frame

    def acquire(self, n_frames: int, timeout: float = 30.0
                ) -> Optional[np.ndarray]:
        """Acquire multiple frames.

        Args:
            n_frames: Number of frames to capture.
            timeout: Total timeout in seconds.

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
        return np.stack(frames)

    # ==========================================================
    # Trigger
    # ==========================================================

    def configure_trigger(
            self,
            source: reg.TriggerSource = reg.TriggerSource.EXTERNAL_SIGNAL,
            activation: reg.TriggerActivation = reg.TriggerActivation.RISING_EDGE,
            selector: reg.TriggerSelector = reg.TriggerSelector.ACQUISITION_START,
            enabled: bool = True) -> None:
        """Configure external trigger.

        Args:
            source: Trigger source (SOFTWARE or EXTERNAL_SIGNAL).
            activation: Edge type (RISING_EDGE, FALLING_EDGE, etc.).
            selector: What to trigger (ACQUISITION_START, FLAGGING, GATING).
            enabled: Enable or disable trigger mode.
        """
        self._check_connected()
        self._gvcp.write_reg(reg.REG_TRIGGER_SELECTOR, int(selector))
        self._gvcp.write_reg(reg.REG_TRIGGER_SOURCE, int(source))
        self._gvcp.write_reg(reg.REG_TRIGGER_ACTIVATION, int(activation))
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

    def buffer_configure(
            self,
            n_sequences: int = 1,
            frames_per_seq: int = 100,
            pre_moi: int = 0,
            moi_source: reg.MemoryBufferMOISource = reg.MemoryBufferMOISource.SOFTWARE
    ) -> None:
        """Configure the internal memory buffer for recording.

        The camera has a 16GB ring buffer that records at full sensor speed
        (up to 3100 fps), independent of the Ethernet link.

        Args:
            n_sequences: Number of recording sequences.
            frames_per_seq: Frames per sequence.
            pre_moi: Frames to keep before the MOI trigger.
            moi_source: What triggers the Moment of Interest
                        (SOFTWARE, EXTERNAL_SIGNAL, ACQUISITION_STARTED).
        """
        self._check_connected()
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_MODE,
                             reg.MemoryBufferMode.ON)
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_NUM_SEQUENCES, n_sequences)
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_SEQ_SIZE, frames_per_seq)
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_PRE_MOI_SIZE, pre_moi)
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_MOI_SOURCE, int(moi_source))

    def buffer_arm(self) -> None:
        """Arm the camera and start acquisition for buffer recording."""
        self._check_connected()
        self._gvcp.write_reg(reg.REG_ACQUISITION_ARM, 1)
        self._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)

    def buffer_fire_moi(self) -> None:
        """Fire software MOI (Moment of Interest) trigger."""
        self._check_connected()
        self._gvcp.write_reg(reg.REG_MEMORY_BUFFER_MOI_SOFTWARE, 1)

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
                        packet_size: int = 9000
                        ) -> Optional[np.ndarray]:
        """Download frames from the internal memory buffer.

        Args:
            sequence: Sequence index to download.
            start_frame: Starting frame ID (0 = first recorded).
            n_frames: Number of frames (0 = all recorded).
            timeout: Total timeout in seconds (0 = auto-calculate).
            bitrate_mbps: Max download bitrate in Mbps (default 1000,
                          camera default is 20 which is very slow).
            packet_size: GVSP packet size in bytes (default 9000).
                         Larger packets = faster download. If you get
                         data loss, try 3000 or 1500.

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
            print("No frames recorded in buffer")
            return None

        first_frame_id = self._gvcp.read_reg(
            reg.REG_MEMORY_BUFFER_SEQ_FIRST_FRAME_ID)
        if start_frame == 0:
            start_frame = first_frame_id

        if timeout <= 0:
            # Estimate at ~250 fps (measured with 1000 Mbps bitrate)
            timeout = max(n_frames / 200.0 * 1.5 + 5.0, 10.0)

        print(f"Downloading {n_frames} frames from buffer...", flush=True)

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
                    print(f"Timeout after {len(frames)}/{n_frames} frames")
                    break
                result = self._gvsp.get_frame(timeout=min(remaining, 10.0))
                if result is not None:
                    frames.append(result)
                    now = time.monotonic()
                    if now - last_progress >= 5.0:
                        elapsed = now - t_start
                        pct = len(frames) / n_frames * 100
                        fps = len(frames) / elapsed
                        eta = (n_frames - len(frames)) / fps if fps > 0 else 0
                        print(f"  {len(frames)}/{n_frames} ({pct:.0f}%) "
                              f"{fps:.0f} fps, ETA {eta:.0f}s", flush=True)
                        last_progress = now
                else:
                    result = self._gvsp.get_frame(timeout=2.0)
                    if result is not None:
                        frames.append(result)
                    else:
                        print(f"Stream stopped at {len(frames)}/{n_frames}")
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

        elapsed = time.monotonic() - t_start
        if frames:
            print(f"Downloaded {len(frames)} frames in {elapsed:.1f}s "
                  f"({len(frames) / elapsed:.0f} fps)")
        else:
            print("Download failed: 0 frames")

        if not frames:
            return None
        return np.stack(frames)

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
