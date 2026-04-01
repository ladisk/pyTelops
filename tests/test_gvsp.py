"""Unit tests for GVSP frame assembly.

Tests frame buffer logic and packet parsing without a physical camera.
"""

import struct
import numpy as np
import pytest

from pyTelops.gvsp import (
    _FrameBuffer, GVSPReceiver, PIXEL_MONO16, PIXEL_MONO8,
    PIXEL_BPP, PIXEL_DTYPE, PACKET_LEADER, PACKET_DATA, PACKET_TRAILER,
)


class TestFrameBuffer:
    """Test frame assembly from packets."""

    def test_empty_buffer(self):
        buf = _FrameBuffer(block_id=1)
        assert not buf.is_complete()
        assert buf.assemble() is None

    def test_assemble_small_frame(self):
        """Assemble a 4x4 Mono16 frame from raw bytes."""
        buf = _FrameBuffer(block_id=1)
        buf.leader_received = True
        buf.pixel_format = PIXEL_MONO16
        buf.width = 4
        buf.height = 4
        buf.trailer_received = True
        buf.expected_packets = 1

        # 4x4 uint16 = 32 bytes
        pixels = np.arange(16, dtype=np.uint16)
        buf.data_packets[1] = pixels.tobytes()

        frame = buf.assemble()
        assert frame is not None
        assert frame.shape == (4, 4)
        assert frame.dtype == np.uint16
        np.testing.assert_array_equal(frame.ravel(), pixels)

    def test_assemble_with_byteswap(self):
        """Byteswap should reverse byte order of each pixel."""
        buf = _FrameBuffer(block_id=1)
        buf.leader_received = True
        buf.pixel_format = PIXEL_MONO16
        buf.width = 2
        buf.height = 2

        pixels = np.array([0x0102, 0x0304, 0x0506, 0x0708], dtype=np.uint16)
        buf.data_packets[1] = pixels.tobytes()

        frame_no_swap = buf.assemble(byteswap=False)
        frame_swapped = buf.assemble(byteswap=True)

        assert frame_no_swap[0, 0] != frame_swapped[0, 0]
        assert frame_swapped[0, 0] == pixels[0].byteswap()

    def test_assemble_mono8(self):
        """Assemble Mono8 frame."""
        buf = _FrameBuffer(block_id=1)
        buf.leader_received = True
        buf.pixel_format = PIXEL_MONO8
        buf.width = 4
        buf.height = 2

        pixels = np.arange(8, dtype=np.uint8)
        buf.data_packets[1] = pixels.tobytes()

        frame = buf.assemble()
        assert frame.shape == (2, 4)
        assert frame.dtype == np.uint8

    def test_missing_packets_padded(self):
        """Missing data should be zero-padded."""
        buf = _FrameBuffer(block_id=1)
        buf.leader_received = True
        buf.pixel_format = PIXEL_MONO16
        buf.width = 4
        buf.height = 4
        buf.trailer_received = True
        buf.expected_packets = 2

        # Only provide half the data
        half = np.ones(8, dtype=np.uint16) * 42
        buf.data_packets[1] = half.tobytes()
        # packet 2 is missing

        frame = buf.assemble()
        assert frame is not None
        assert frame.shape == (4, 4)
        # First 8 pixels should be 42, rest zero-padded
        assert frame.ravel()[0] == 42
        assert frame.ravel()[8] == 0

    def test_multi_packet_ordering(self):
        """Packets should be assembled in packet_id order."""
        buf = _FrameBuffer(block_id=1)
        buf.leader_received = True
        buf.pixel_format = PIXEL_MONO16
        buf.width = 4
        buf.height = 2

        part1 = np.array([1, 2, 3, 4], dtype=np.uint16)
        part2 = np.array([5, 6, 7, 8], dtype=np.uint16)

        # Insert out of order
        buf.data_packets[2] = part2.tobytes()
        buf.data_packets[1] = part1.tobytes()

        frame = buf.assemble()
        np.testing.assert_array_equal(
            frame.ravel(), [1, 2, 3, 4, 5, 6, 7, 8])

    def test_is_complete(self):
        buf = _FrameBuffer(block_id=1)
        assert not buf.is_complete()

        buf.leader_received = True
        assert not buf.is_complete()

        buf.trailer_received = True
        buf.expected_packets = 2
        assert not buf.is_complete()

        buf.data_packets[1] = b"\x00"
        assert not buf.is_complete()

        buf.data_packets[2] = b"\x00"
        assert buf.is_complete()


class TestContiguousRanges:
    """Test packet ID range grouping for resend requests."""

    def test_empty(self):
        assert GVSPReceiver._contiguous_ranges([]) == []

    def test_single(self):
        assert GVSPReceiver._contiguous_ranges([5]) == [(5, 5)]

    def test_contiguous(self):
        assert GVSPReceiver._contiguous_ranges([1, 2, 3]) == [(1, 3)]

    def test_gaps(self):
        assert GVSPReceiver._contiguous_ranges([1, 2, 5, 6, 7, 10]) == [
            (1, 2), (5, 7), (10, 10)]

    def test_all_separate(self):
        assert GVSPReceiver._contiguous_ranges([1, 3, 5]) == [
            (1, 1), (3, 3), (5, 5)]


class TestPixelFormats:
    """Test pixel format definitions."""

    def test_mono16_properties(self):
        assert PIXEL_BPP[PIXEL_MONO16] == 2
        assert PIXEL_DTYPE[PIXEL_MONO16] == np.uint16

    def test_mono8_properties(self):
        assert PIXEL_BPP[PIXEL_MONO8] == 1
        assert PIXEL_DTYPE[PIXEL_MONO8] == np.uint8

    def test_all_formats_have_bpp_and_dtype(self):
        for fmt in PIXEL_BPP:
            assert fmt in PIXEL_DTYPE
