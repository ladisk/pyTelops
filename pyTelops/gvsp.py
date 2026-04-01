"""
GigE Vision Streaming Protocol (GVSP) receiver.

Receives image frames streamed from a GigE Vision camera over UDP.
The camera sends frames as a sequence of packets:
  - Leader packet: image metadata (dimensions, pixel format, timestamp)
  - Data packets: raw pixel data chunks
  - Trailer packet: signals frame complete

This module is generic — it works with any GigE Vision camera.
Byte order is configurable (Telops sends little-endian, most others big-endian).
"""

import logging
import math
import socket
import struct
import threading
import time
from collections import defaultdict
from queue import Queue, Empty
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# GVSP packet types
PACKET_LEADER = 0x01
PACKET_TRAILER = 0x02
PACKET_DATA = 0x03

# Leader payload type IDs
PAYLOAD_IMAGE = 0x0001

# Standard pixel format codes (GenICam PFNC)
PIXEL_MONO8 = 0x01080001
PIXEL_MONO16 = 0x01100007
PIXEL_MONO10 = 0x01100003
PIXEL_MONO12 = 0x01100005
PIXEL_MONO14 = 0x01100025

PIXEL_BPP = {
    PIXEL_MONO8: 1,
    PIXEL_MONO10: 2,
    PIXEL_MONO12: 2,
    PIXEL_MONO14: 2,
    PIXEL_MONO16: 2,
}

PIXEL_DTYPE = {
    PIXEL_MONO8: np.uint8,
    PIXEL_MONO10: np.uint16,
    PIXEL_MONO12: np.uint16,
    PIXEL_MONO14: np.uint16,
    PIXEL_MONO16: np.uint16,
}


class _FrameBuffer:
    """Accumulates packets for a single frame."""

    def __init__(self, block_id: int):
        self.block_id = block_id
        self.timestamp = 0
        self.pixel_format = 0
        self.width = 0
        self.height = 0
        self.payload_type = 0
        self.data_packets: dict[int, bytes] = {}
        self.leader_received = False
        self.trailer_received = False
        self.expected_packets = 0
        self.created_at = time.monotonic()

    def is_complete(self) -> bool:
        if not self.leader_received or not self.trailer_received:
            return False
        if self.expected_packets > 0:
            return len(self.data_packets) >= self.expected_packets
        return True

    def assemble(self, byteswap: bool = False) -> Optional[np.ndarray]:
        """Assemble data packets into a numpy array.

        Args:
            byteswap: If True, swap byte order (for big-endian cameras).
                      Telops sends little-endian (native x86) so False.
        """
        if not self.leader_received:
            return None

        sorted_ids = sorted(self.data_packets.keys())
        raw = b"".join(self.data_packets[pid] for pid in sorted_ids)

        bpp = PIXEL_BPP.get(self.pixel_format, 2)
        dtype = PIXEL_DTYPE.get(self.pixel_format, np.uint16)

        expected_size = self.width * self.height * bpp
        if len(raw) < expected_size:
            raw = raw + b"\x00" * (expected_size - len(raw))

        arr = np.frombuffer(raw[:expected_size], dtype=dtype)

        if byteswap:
            arr = arr.byteswap()

        try:
            return arr.reshape((self.height, self.width))
        except ValueError:
            return arr


class GVSPReceiver:
    """Receives GVSP image frames on a UDP socket.

    Args:
        local_ip: Local IP to bind to.
        local_port: Local UDP port (0 = auto-assign).
        max_queue: Max completed frames to buffer.
        stale_timeout: Seconds before dropping incomplete frames.
        gvcp_client: Optional GVCPClient for packet resend requests.
        packet_size: Network packet size (default 1500 = standard MTU).
        byteswap: Swap pixel byte order (False for Telops, True for most others).
    """

    def __init__(self, local_ip: str = "", local_port: int = 0,
                 max_queue: int = 30, stale_timeout: float = 5.0,
                 gvcp_client=None, packet_size: int = 1500,
                 byteswap: bool = False):
        self.local_ip = local_ip
        self.stale_timeout = stale_timeout
        self.byteswap = byteswap
        self._gvcp = gvcp_client
        self._packet_data_size = packet_size - 8

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,
                                  16 * 1024 * 1024)
        except OSError:
            pass
        self._sock.bind((local_ip, local_port))
        self._sock.settimeout(1.0)

        self._port = self._sock.getsockname()[1]
        self._frame_queue: Queue = Queue(maxsize=max_queue)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._frame_buffers: dict[int, _FrameBuffer] = {}
        self._resend_stats = {"requested": 0, "recovered": 0, "failed": 0}
        self.resend_enabled = True

    @property
    def port(self) -> int:
        """UDP port the receiver is bound to."""
        return self._port

    def start(self):
        """Start the receiver thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._receive_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the receiver thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        self._frame_buffers.clear()

    def close(self):
        """Stop and close the socket."""
        self.stop()
        self._sock.close()

    def get_frame(self, timeout: float = 5.0) -> Optional[np.ndarray]:
        """Block until a frame is available, return as numpy array."""
        result = self.get_frame_with_info(timeout)
        if result is None:
            return None
        return result[0]

    def get_frame_with_info(self, timeout: float = 5.0
                            ) -> Optional[tuple[np.ndarray, dict]]:
        """Block until a frame is available, return (array, metadata)."""
        try:
            return self._frame_queue.get(timeout=timeout)
        except Empty:
            return None

    # --- Internal ---

    def _receive_loop(self):
        """Main receiver loop running in background thread."""
        while not self._stop_event.is_set():
            try:
                data, addr = self._sock.recvfrom(65536)
            except socket.timeout:
                self._cleanup_stale()
                continue
            except OSError:
                break

            if len(data) < 8:
                continue

            self._parse_packet(data)
            self._cleanup_stale()

    def _parse_packet(self, data: bytes):
        """Parse a single GVSP packet."""
        status = struct.unpack(">H", data[0:2])[0]
        format_byte = data[4]
        extended = bool(format_byte & 0x08)

        if extended:
            if len(data) < 20:
                return
            block_id = struct.unpack(">Q", data[4:12])[0]
            packet_format = struct.unpack(">I", data[12:16])[0]
            packet_type = (packet_format >> 24) & 0x0F
            packet_id = struct.unpack(">I", data[16:20])[0]
            header_size = 20
        else:
            block_id = struct.unpack(">H", data[2:4])[0]
            packet_type = format_byte & 0x07
            packet_id = (data[5] << 16) | (data[6] << 8) | data[7]
            header_size = 8

        payload = data[header_size:]

        if packet_type == PACKET_LEADER:
            self._handle_leader(block_id, payload)
        elif packet_type == PACKET_DATA:
            self._handle_data(block_id, packet_id, payload)
        elif packet_type == PACKET_TRAILER:
            self._handle_trailer(block_id, payload)

    def _handle_leader(self, block_id: int, payload: bytes):
        """Parse leader packet with image metadata."""
        buf = _FrameBuffer(block_id)

        if len(payload) >= 24:
            buf.payload_type = struct.unpack(">H", payload[2:4])[0]
            buf.timestamp = struct.unpack(">Q", payload[4:12])[0]
            buf.pixel_format = struct.unpack(">I", payload[12:16])[0]
            buf.width = struct.unpack(">I", payload[16:20])[0]
            buf.height = struct.unpack(">I", payload[20:24])[0]

        buf.leader_received = True
        self._frame_buffers[block_id] = buf

    def _handle_data(self, block_id: int, packet_id: int, payload: bytes):
        """Store data packet payload."""
        if block_id not in self._frame_buffers:
            self._frame_buffers[block_id] = _FrameBuffer(block_id)
        self._frame_buffers[block_id].data_packets[packet_id] = payload

    def _handle_trailer(self, block_id: int, payload: bytes):
        """Handle trailer packet, request resend for missing packets, emit frame."""
        if block_id not in self._frame_buffers:
            return

        buf = self._frame_buffers[block_id]
        buf.trailer_received = True

        if buf.leader_received and buf.width > 0 and buf.height > 0:
            bpp = PIXEL_BPP.get(buf.pixel_format, 2)
            total_bytes = buf.width * buf.height * bpp
            buf.expected_packets = math.ceil(total_bytes / self._packet_data_size)

        # Check for missing packets and request resend
        if buf.expected_packets > 0:
            expected_ids = set(range(1, buf.expected_packets + 1))
            received_ids = set(buf.data_packets.keys())
            missing = expected_ids - received_ids

            if missing and self._gvcp is not None and self.resend_enabled:
                missing = self._request_resend(buf, missing)

            if missing:
                self._resend_stats["failed"] += len(missing)
                logger.warning(
                    f"Frame {block_id}: {len(missing)}/{buf.expected_packets} "
                    f"packets unrecoverable")

        # Assemble and emit
        frame = buf.assemble(byteswap=self.byteswap)
        if frame is not None:
            info = {
                "block_id": buf.block_id,
                "timestamp": buf.timestamp,
                "pixel_format": buf.pixel_format,
                "width": buf.width,
                "height": buf.height,
                "missing_packets": (buf.expected_packets - len(buf.data_packets)
                                    if buf.expected_packets > 0 else 0),
            }
            if self._frame_queue.full():
                try:
                    self._frame_queue.get_nowait()
                except Empty:
                    pass
            self._frame_queue.put((frame, info))

        self._frame_buffers.pop(block_id, None)

    def _request_resend(self, buf: _FrameBuffer, missing: set[int],
                        max_attempts: int = 2, wait_ms: float = 200.0
                        ) -> set[int]:
        """Request retransmission of missing packets."""
        for attempt in range(max_attempts):
            ranges = self._contiguous_ranges(sorted(missing))
            for first, last in ranges:
                try:
                    self._gvcp.send_packetresend(buf.block_id, first, last)
                    self._resend_stats["requested"] += (last - first + 1)
                except Exception as e:
                    logger.debug(f"Resend request failed: {e}")
                    return missing

            deadline = time.monotonic() + wait_ms / 1000.0
            while time.monotonic() < deadline:
                still_missing = missing - set(buf.data_packets.keys())
                if not still_missing:
                    self._resend_stats["recovered"] += len(missing)
                    return set()
                time.sleep(0.005)

            missing = missing - set(buf.data_packets.keys())
            if not missing:
                self._resend_stats["recovered"] += len(missing)
                return set()

        return missing

    @staticmethod
    def _contiguous_ranges(ids: list[int]) -> list[tuple[int, int]]:
        """Group sorted packet IDs into contiguous (first, last) ranges."""
        if not ids:
            return []
        ranges = []
        first = last = ids[0]
        for pid in ids[1:]:
            if pid == last + 1:
                last = pid
            else:
                ranges.append((first, last))
                first = last = pid
        ranges.append((first, last))
        return ranges

    def _cleanup_stale(self):
        """Remove frame buffers that have been incomplete for too long."""
        now = time.monotonic()
        stale = [bid for bid, buf in self._frame_buffers.items()
                 if now - buf.created_at > self.stale_timeout]
        for bid in stale:
            self._frame_buffers.pop(bid, None)
