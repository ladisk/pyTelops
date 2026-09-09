"""Tests for the per-frame Telops header parser.

The headers are built here with struct.pack at the documented offsets and
embedded into a synthetic uint16 frame stack, so no camera is needed.
"""

from __future__ import annotations

import struct
from datetime import timezone

import numpy as np
import pytest

from pyTelops.header import (
    HDR_BUFFERING_FLAG,
    HDR_CAL_MODE,
    HDR_DATA_EXP,
    HDR_DATA_OFFSET,
    HDR_EXPOSURE_TIME,
    HDR_FRAME_ID,
    HDR_FRAME_RATE,
    HDR_HEADER_LENGTH,
    HDR_HEIGHT,
    HDR_POSIX_TIME,
    HDR_SIGNATURE,
    HDR_SUBSECOND,
    HDR_WIDTH,
    HDR_XML_MAJOR,
    HDR_XML_MINOR,
    HEADER_BYTES,
    SIGNATURE,
    BufferingFlag,
    header_frame_ids,
    header_timestamps,
    parse_header,
    parse_headers,
)

WIDTH = 64
HEIGHT = 4
HEADER_ROWS = 2


def _make_header_bytes(
    *,
    version=(12, 7),
    header_length=256,
    frame_id=1000,
    data_offset=273.15,
    data_exp=-8,
    exposure_raw=11016,
    calibration_mode=2,
    width=WIDTH,
    height=HEIGHT + HEADER_ROWS,
    frame_rate_raw=2_000_000,
    buffering_flag=0,
    posix_time=1_757_318_400,
    subsecond_raw=1_234_567,
    signature=SIGNATURE,
):
    """Build one synthetic 256-byte header, zero-filled outside the fields.

    Parameters
    ----------
    version : tuple of int
        Header version as ``(major, minor)``.
    header_length, frame_id, calibration_mode, buffering_flag, posix_time : int
        Raw integer field values.
    exposure_raw, frame_rate_raw, subsecond_raw : int
        Raw uint32 field values, before the documented scaling.
    data_offset : float
        Raw float32 offset.
    data_exp : int
        Raw int8 exponent.
    width, height : int
        Raw geometry fields.
    signature : bytes
        Signature to write at byte 0, so bad-signature cases can be built.

    Returns
    -------
    bytearray
        A 256-byte header.
    """
    raw = bytearray(HEADER_BYTES)
    major, minor = version
    raw[HDR_SIGNATURE : HDR_SIGNATURE + 2] = signature
    raw[HDR_XML_MINOR] = minor
    raw[HDR_XML_MAJOR] = major
    struct.pack_into("<H", raw, HDR_HEADER_LENGTH, header_length)
    struct.pack_into("<I", raw, HDR_FRAME_ID, frame_id)
    struct.pack_into("<f", raw, HDR_DATA_OFFSET, data_offset)
    struct.pack_into("<b", raw, HDR_DATA_EXP, data_exp)
    struct.pack_into("<I", raw, HDR_EXPOSURE_TIME, exposure_raw)
    raw[HDR_CAL_MODE] = calibration_mode
    struct.pack_into("<H", raw, HDR_WIDTH, width)
    struct.pack_into("<H", raw, HDR_HEIGHT, height)
    struct.pack_into("<I", raw, HDR_FRAME_RATE, frame_rate_raw)
    raw[HDR_BUFFERING_FLAG] = buffering_flag
    struct.pack_into("<I", raw, HDR_POSIX_TIME, posix_time)
    struct.pack_into("<I", raw, HDR_SUBSECOND, subsecond_raw)
    return raw


def _make_frames(headers, width=WIDTH, height=HEIGHT):
    """Embed headers into a synthetic uint16 frame stack.

    Parameters
    ----------
    headers : list of bytes
        One 256-byte header per frame.
    width, height : int
        Image size without the header rows.

    Returns
    -------
    numpy.ndarray
        uint16 array of shape ``(N, height + 2, width)``.
    """
    n = len(headers)
    frames = np.zeros((n, height + HEADER_ROWS, width), dtype="<u2")
    for i, header in enumerate(headers):
        row_bytes = np.frombuffer(bytes(header), dtype=np.uint8)
        rows = np.zeros(HEADER_ROWS * width * 2, dtype=np.uint8)
        rows[: len(row_bytes)] = row_bytes
        frames[i, :HEADER_ROWS, :] = rows.view("<u2").reshape(HEADER_ROWS, width)
        frames[i, HEADER_ROWS:, :] = i + 1
    return frames


def test_header_fits_the_two_rows_exactly():
    """Two rows of 64 uint16 pixels are exactly the 256 header bytes."""
    assert HEADER_ROWS * WIDTH * 2 == HEADER_BYTES


def test_all_fields_round_trip_v12_7():
    """Every parsed field of a 12.7 header comes back scaled."""
    frames = _make_frames([_make_header_bytes()])
    header = parse_header(frames[0])

    assert header.header_version == (12, 7)
    assert header.header_length == 256
    assert header.frame_id == 1000
    assert header.data_offset == pytest.approx(273.15, rel=1e-6)
    assert header.data_exp == -8
    assert header.exposure_time_us == pytest.approx(110.16)
    assert header.calibration_mode == 2
    assert header.width == WIDTH
    assert header.height == HEIGHT + HEADER_ROWS
    assert header.frame_rate_hz == pytest.approx(2000.0)
    assert header.posix_time == 1_757_318_400
    assert header.subsecond_us == pytest.approx(123456.7, rel=1e-6)


def test_all_fields_round_trip_v12_9():
    """A 12.9 header parses the same fields plus the buffering flag."""
    frames = _make_frames(
        [
            _make_header_bytes(
                version=(12, 9),
                frame_id=42,
                calibration_mode=255,
                buffering_flag=2,
                exposure_raw=2500,
                frame_rate_raw=500_000,
            )
        ]
    )
    header = parse_header(frames[0])

    assert header.header_version == (12, 9)
    assert header.frame_id == 42
    assert header.calibration_mode == 255
    assert header.exposure_time_us == pytest.approx(25.0)
    assert header.frame_rate_hz == pytest.approx(500.0)
    assert header.buffering_flag is BufferingFlag.MOI


def test_buffering_flag_is_none_below_12_9():
    """Byte 74 is reserved before header 12.9, so the flag stays None."""
    frames = _make_frames([_make_header_bytes(version=(12, 7), buffering_flag=2)])
    assert parse_header(frames[0]).buffering_flag is None


def test_buffering_flag_variants_on_12_9():
    """Known values map to the enum, unknown ones stay raw ints."""
    headers = [_make_header_bytes(version=(12, 9), buffering_flag=v) for v in (0, 1, 3, 200)]
    parsed = parse_headers(_make_frames(headers))

    assert parsed[0].buffering_flag is BufferingFlag.NONE
    assert parsed[1].buffering_flag is BufferingFlag.PRE_MOI
    assert parsed[2].buffering_flag is BufferingFlag.POST_MOI
    assert parsed[3].buffering_flag == 200
    assert not isinstance(parsed[3].buffering_flag, BufferingFlag)


def test_timestamp_and_datetime_properties():
    """timestamp adds the sub-second part, datetime is UTC-aware."""
    frames = _make_frames([_make_header_bytes(posix_time=1_757_318_400, subsecond_raw=5_000_000)])
    header = parse_header(frames[0])

    assert header.subsecond_us == pytest.approx(500000.0)
    assert header.timestamp == pytest.approx(1_757_318_400.5, abs=1e-6)

    dt = header.datetime
    assert dt.tzinfo is timezone.utc
    assert dt.timestamp() == pytest.approx(header.timestamp, abs=1e-6)
    assert dt.microsecond == pytest.approx(500000, abs=2)


def test_parse_headers_returns_one_per_frame():
    """A stack of N frames gives N headers in input order."""
    headers = [_make_header_bytes(frame_id=100 + i) for i in range(5)]
    parsed = parse_headers(_make_frames(headers))

    assert len(parsed) == 5
    assert [h.frame_id for h in parsed] == [100, 101, 102, 103, 104]


def test_parse_headers_accepts_a_single_frame():
    """A 2-D frame gives a one-element list."""
    frames = _make_frames([_make_header_bytes(frame_id=7)])
    parsed = parse_headers(frames[0])

    assert len(parsed) == 1
    assert parsed[0].frame_id == 7


def test_header_timestamps_matches_parse_headers():
    """The vectorised timestamps equal the per-header ones."""
    headers = [
        _make_header_bytes(posix_time=1_757_318_400 + i // 2, subsecond_raw=i * 1_000_000)
        for i in range(6)
    ]
    frames = _make_frames(headers)

    fast = header_timestamps(frames)
    slow = np.array([h.timestamp for h in parse_headers(frames)])

    assert fast.dtype == np.float64
    assert fast.shape == (6,)
    assert np.allclose(fast, slow, atol=1e-6)


def test_header_timestamps_relative_starts_at_zero():
    """relative=True subtracts the first timestamp."""
    headers = [
        _make_header_bytes(posix_time=1_757_318_400, subsecond_raw=i * 10_000) for i in range(4)
    ]
    frames = _make_frames(headers)

    absolute = header_timestamps(frames)
    relative = header_timestamps(frames, relative=True)

    assert relative[0] == 0.0
    # Absolute timestamps carry about 0.2 us of float rounding, relative ones do not.
    assert np.allclose(relative, absolute - absolute[0], atol=1e-6)
    assert np.allclose(np.diff(relative), 0.001, atol=1e-12)


def test_header_timestamps_relative_across_a_second_rollover():
    """Relative spacing stays exact when the POSIX second increments."""
    headers = [
        _make_header_bytes(
            posix_time=1_757_318_400 + i // 2,
            subsecond_raw=(i % 2) * 5_000_000,
        )
        for i in range(6)
    ]
    relative = header_timestamps(_make_frames(headers), relative=True)

    assert list(relative) == [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]


def test_header_timestamps_accepts_a_single_frame():
    """A 2-D input gives shape (1,)."""
    frames = _make_frames([_make_header_bytes()])
    timestamps = header_timestamps(frames[0])

    assert timestamps.shape == (1,)
    assert timestamps[0] == pytest.approx(parse_header(frames[0]).timestamp, abs=1e-6)


def test_header_frame_ids():
    """Frame ids come back as uint32 in input order."""
    headers = [_make_header_bytes(frame_id=i) for i in (0, 1, 5, 4_000_000_000)]
    ids = header_frame_ids(_make_frames(headers))

    assert ids.dtype == np.uint32
    assert ids.shape == (4,)
    assert list(ids) == [0, 1, 5, 4_000_000_000]

    single = header_frame_ids(_make_frames([_make_header_bytes(frame_id=9)])[0])
    assert single.shape == (1,)
    assert single[0] == 9


def test_empty_stack_gives_empty_arrays():
    """An empty download is not an error: N == 0 gives shape (0,)."""
    frames = np.zeros((0, HEIGHT + HEADER_ROWS, WIDTH), dtype="<u2")

    timestamps = header_timestamps(frames)
    assert timestamps.shape == (0,)
    assert timestamps.dtype == np.float64

    relative = header_timestamps(frames, relative=True)
    assert relative.shape == (0,)

    ids = header_frame_ids(frames)
    assert ids.shape == (0,)
    assert ids.dtype == np.uint32

    assert parse_headers(frames) == []


def test_bad_signature_raises_value_error():
    """A frame without the "TC" signature is rejected."""
    frames = _make_frames([_make_header_bytes(signature=b"XX")])
    with pytest.raises(ValueError, match="signature"):
        parse_header(frames[0])


def test_too_short_input_raises_value_error():
    """Fewer than 256 header bytes is rejected."""
    with pytest.raises(ValueError, match="at least 256"):
        parse_header(np.zeros((2, 8), dtype="<u2"))

    with pytest.raises(ValueError, match="at least 256"):
        parse_header(bytes(_make_header_bytes())[:100])


def test_wrong_dtype_raises_type_error():
    """Only little-endian uint16 raw frames are accepted."""
    frames = _make_frames([_make_header_bytes()])

    with pytest.raises(TypeError, match="uint16"):
        parse_header(frames[0].astype(np.float32))

    with pytest.raises(TypeError, match="little-endian"):
        parse_header(frames[0].astype(">u2"))

    with pytest.raises(TypeError, match="uint16"):
        header_timestamps(frames.astype(np.int16))


def test_bytes_input_is_accepted():
    """parse_header also takes raw bytes and a 1-D uint8 array."""
    raw = bytes(_make_header_bytes(frame_id=321))

    assert parse_header(raw).frame_id == 321
    assert parse_header(np.frombuffer(raw, dtype=np.uint8)).frame_id == 321
