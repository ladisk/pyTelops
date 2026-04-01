"""Unit tests for GVCP protocol encoding/decoding.

These tests verify packet construction and parsing without needing
a physical camera.
"""

import struct
import pytest

from pyTelops.gvcp import (
    GVCPClient, GVCPError, GVCP_KEY, FLAG_ACK, FLAG_BROADCAST,
    CMD_DISCOVERY, CMD_READREG, CMD_WRITEREG, CMD_READMEM,
    STATUS_SUCCESS, STATUS_NAMES, READMEM_CHUNK,
)


class TestGVCPPacketFormat:
    """Test GVCP packet header construction."""

    def test_header_size(self):
        """GVCP header is exactly 8 bytes."""
        header = struct.pack(">BBHHH", GVCP_KEY, FLAG_ACK,
                             CMD_READREG, 4, 1)
        assert len(header) == 8

    def test_header_key(self):
        """First byte is always 0x42."""
        header = struct.pack(">BBHHH", GVCP_KEY, FLAG_ACK,
                             CMD_READREG, 4, 1)
        assert header[0] == 0x42

    def test_discovery_packet(self):
        """Discovery packet: broadcast flag, zero payload."""
        pkt = struct.pack(">BBHHH", GVCP_KEY, FLAG_BROADCAST,
                          CMD_DISCOVERY, 0, 0xFFFF)
        assert len(pkt) == 8
        assert struct.unpack(">H", pkt[2:4])[0] == CMD_DISCOVERY
        assert struct.unpack(">H", pkt[4:6])[0] == 0  # no payload

    def test_readreg_payload(self):
        """READREG payload is a 4-byte address."""
        addr = 0xD300
        payload = struct.pack(">I", addr)
        assert len(payload) == 4
        assert struct.unpack(">I", payload)[0] == addr

    def test_writereg_payload(self):
        """WRITEREG payload is address + value (8 bytes)."""
        addr = 0xD314
        value = 1
        payload = struct.pack(">II", addr, value)
        assert len(payload) == 8

    def test_readmem_payload(self):
        """READMEM payload is address + reserved + count."""
        addr = 0x0200
        size = 512
        payload = struct.pack(">IHH", addr, 0, size)
        assert len(payload) == 8

    def test_req_id_is_two_bytes(self):
        """req_id must be 2 bytes (H), not 4 bytes (I)."""
        # This was bug #1 in the original driver
        req_id = 42
        header = struct.pack(">BBHHH", GVCP_KEY, FLAG_ACK,
                             CMD_READREG, 4, req_id)
        parsed_id = struct.unpack(">H", header[6:8])[0]
        assert parsed_id == req_id


class TestGVCPError:
    """Test error handling."""

    def test_error_with_known_status(self):
        err = GVCPError("test", 0x8006)
        assert err.status == 0x8006
        assert err.status_name == "ACCESS_DENIED"
        assert "ACCESS_DENIED" in str(err)

    def test_error_with_unknown_status(self):
        err = GVCPError("test", 0x9999)
        assert "UNKNOWN" in err.status_name

    def test_error_with_zero_status(self):
        err = GVCPError("test", 0)
        assert err.status_name == "SUCCESS"

    def test_all_known_statuses(self):
        """Every status in STATUS_NAMES has a human-readable name."""
        for code, name in STATUS_NAMES.items():
            assert isinstance(name, str)
            assert len(name) > 0


class TestGVCPClientInit:
    """Test client initialization (no network)."""

    def test_default_init(self):
        client = GVCPClient("192.168.1.1")
        assert client.camera_ip == "192.168.1.1"
        assert client.local_ip == ""
        assert client.timeout == 2.0
        assert not client._connected

    def test_init_with_options(self):
        client = GVCPClient("10.0.0.1", local_ip="10.0.0.2", timeout=5.0)
        assert client.camera_ip == "10.0.0.1"
        assert client.local_ip == "10.0.0.2"
        assert client.timeout == 5.0

    def test_readmem_chunk_size(self):
        """READMEM chunk must be <= 512 for safe Ethernet."""
        assert READMEM_CHUNK <= 512


class TestConstants:
    """Test protocol constants."""

    def test_gvcp_port(self):
        from pyTelops.gvcp import GVCP_PORT
        assert GVCP_PORT == 3956

    def test_command_codes(self):
        assert CMD_DISCOVERY == 0x0002
        assert CMD_READREG == 0x0080
        assert CMD_WRITEREG == 0x0082
        assert CMD_READMEM == 0x0084
