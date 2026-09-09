"""Parsing of the per-frame Telops image header.

Every frame the camera sends carries a 256-byte header in its first two
rows (``Camera.HEADER_ROWS``). The camera writes it at exposure time, so
the timestamp it holds has no host-side delay. All fields are stored
little-endian.

The offsets and scale factors follow the Telops "HCC Header Reference"
document (revisions v12.7 and v13.4) and the reader in ``fasthcc``
(``fasthcc/header.py``), which is validated against real Telops HCC
files. The fields parsed here are the ones the driver needs
(identification, geometry, timing and the moment-of-interest flag); the
header holds many more.

Exposure time, acquisition frame rate and the sub-second time are
uint32 counters, not floats. The Buffering Flag at byte 74 first appears
in Header Reference v12.9; it was checked against the whole document
series v12.7 to v13.4 and is reserved before that.

Examples
--------
>>> frames = cam.buffer_download(strip_header=False)  # doctest: +SKIP
>>> headers = parse_headers(frames)  # doctest: +SKIP
>>> headers[0].datetime  # doctest: +SKIP
datetime.datetime(2026, 9, 8, 10, 12, 3, 415200, tzinfo=datetime.timezone.utc)
>>> t = header_timestamps(frames, relative=True)  # doctest: +SKIP
"""

from __future__ import annotations

import struct
import sys
from dataclasses import dataclass
from datetime import datetime as _datetime
from datetime import timezone as _timezone
from enum import IntEnum

import numpy as np

__all__ = [
    "HEADER_BYTES",
    "SIGNATURE",
    "HDR_SIGNATURE",
    "HDR_XML_MINOR",
    "HDR_XML_MAJOR",
    "HDR_HEADER_LENGTH",
    "HDR_FRAME_ID",
    "HDR_DATA_OFFSET",
    "HDR_DATA_EXP",
    "HDR_EXPOSURE_TIME",
    "HDR_CAL_MODE",
    "HDR_WIDTH",
    "HDR_HEIGHT",
    "HDR_FRAME_RATE",
    "HDR_BUFFERING_FLAG",
    "HDR_POSIX_TIME",
    "HDR_SUBSECOND",
    "BufferingFlag",
    "FrameHeader",
    "parse_header",
    "parse_headers",
    "header_timestamps",
    "header_frame_ids",
]

#: Size of the image header in bytes.
HEADER_BYTES = 256

#: Signature at the start of every valid header.
SIGNATURE = b"TC"

# Byte offsets of the parsed fields inside the 256-byte header.
HDR_SIGNATURE = 0  # char[2]: "TC"
HDR_XML_MINOR = 2  # uint8: XML / header minor version
HDR_XML_MAJOR = 3  # uint8: XML / header major version
HDR_HEADER_LENGTH = 4  # uint16: header length in bytes
HDR_FRAME_ID = 8  # uint32: frame id
HDR_DATA_OFFSET = 12  # float32: additive offset (273.15 for RT Kelvin)
HDR_DATA_EXP = 16  # int8: exponent (typically -8 for RT)
HDR_EXPOSURE_TIME = 24  # uint32: exposure time, /100 -> us
HDR_CAL_MODE = 28  # uint8: calibration mode (2=RT, 1=NUC, 255=Raw)
HDR_WIDTH = 32  # uint16: image width in pixels
HDR_HEIGHT = 34  # uint16: image height in pixels
HDR_FRAME_RATE = 44  # uint32: acquisition frame rate, /1000 -> Hz
HDR_BUFFERING_FLAG = 74  # uint8: buffering state, header >= 12.9 only
HDR_POSIX_TIME = 100  # uint32: POSIX seconds, UTC
HDR_SUBSECOND = 104  # uint32: 100 ns ticks, /10 -> us

#: First header version that defines the buffering flag at byte 74.
_BUFFERING_FLAG_SINCE = (12, 9)

_NATIVE_LITTLE = sys.byteorder == "little"


class BufferingFlag(IntEnum):
    """Buffering state of a frame relative to the moment of interest (MOI).

    The camera sets this in the onboard memory buffer. It exists from
    header version 12.9 on; older cameras keep byte 74 reserved.

    The values are ``NONE`` (0, the frame is not part of a buffered
    sequence), ``PRE_MOI`` (1, recorded before the moment of interest),
    ``MOI`` (2, the frame that carries it) and ``POST_MOI`` (3, recorded
    after it).
    """

    NONE = 0
    PRE_MOI = 1
    MOI = 2
    POST_MOI = 3


@dataclass(frozen=True)
class FrameHeader:
    """Decoded Telops image header of one frame.

    Values are already scaled to the units given below.

    Parameters
    ----------
    header_version : tuple of int
        Header version as ``(major, minor)``, taken from the device XML
        version bytes.
    header_length : int
        Length of the image header in bytes, as reported by the camera.
    frame_id : int
        Frame counter of the camera.
    data_offset : float
        Additive offset applied when converting raw counts to physical
        units (273.15 for RT in Kelvin).
    data_exp : int
        Exponent of the raw-to-physical conversion (typically -8 for RT).
    exposure_time_us : float
        Exposure time in microseconds.
    calibration_mode : int
        Calibration mode: 255 Raw, 1 NUC, 2 RT, 3 IBR, 4 IBI.
    width : int
        Image width in pixels.
    height : int
        Image height in pixels, as the device reports it. On this camera
        that includes the two header rows.
    frame_rate_hz : float
        Acquisition frame rate in hertz.
    buffering_flag : BufferingFlag or int or None
        Position of the frame relative to the moment of interest, or
        None when the header version is older than 12.9 (byte 74 is
        reserved there and must not be trusted). A value outside 0-3 is
        kept as a plain int.
    posix_time : int
        Whole POSIX seconds (UTC) of the exposure.
    subsecond_us : float
        Sub-second part of the timestamp in microseconds.

    Notes
    -----
    The sub-second field is a uint32 counter of 100 ns ticks. Once it is
    added to a present-day POSIX second in float64, the absolute
    resolution of :attr:`timestamp` is about 0.2 us. Use
    :func:`header_timestamps` with ``relative=True`` when you need the
    small time differences without that loss.
    """

    header_version: tuple[int, int]
    header_length: int
    frame_id: int
    data_offset: float
    data_exp: int
    exposure_time_us: float
    calibration_mode: int
    width: int
    height: int
    frame_rate_hz: float
    buffering_flag: BufferingFlag | int | None
    posix_time: int
    subsecond_us: float

    @property
    def timestamp(self) -> float:
        """float: Exposure time as POSIX seconds (UTC), sub-second included."""
        return self.posix_time + self.subsecond_us * 1e-6

    @property
    def datetime(self) -> _datetime:
        """datetime.datetime: Exposure time as a timezone-aware UTC datetime."""
        return _datetime.fromtimestamp(self.timestamp, tz=_timezone.utc)


def _check_uint16(arr: np.ndarray) -> None:
    """Raise TypeError unless ``arr`` is uint16 in native or little-endian order.

    Parameters
    ----------
    arr : numpy.ndarray
        Array to check.

    Raises
    ------
    TypeError
        If the dtype is not a 2-byte unsigned integer, or if it is
        big-endian.
    """
    dtype = arr.dtype
    if dtype.kind != "u" or dtype.itemsize != 2:
        raise TypeError(f"raw frames must be uint16, got dtype {dtype}")
    if dtype.byteorder == ">" or (dtype.byteorder == "=" and not _NATIVE_LITTLE):
        raise TypeError(f"raw frames must be little-endian uint16, got dtype {dtype}")


def _header_bytes(frame: np.ndarray | bytes | bytearray | memoryview) -> bytes:
    """Return the first 256 header bytes of a single frame.

    Parameters
    ----------
    frame : numpy.ndarray or bytes-like
        Either a 2-D uint16 frame with the header rows still attached
        (shape ``(H + 2, W)``), a 1-D uint8 array, or a bytes-like
        object of at least 256 bytes.

    Returns
    -------
    bytes
        Exactly 256 bytes of header data.

    Raises
    ------
    TypeError
        If a 2-D array is not little-endian uint16, or if the input is
        neither an array nor bytes-like.
    ValueError
        If the input holds fewer than 256 header bytes, or has an
        unsupported number of dimensions.
    """
    if isinstance(frame, (bytes, bytearray, memoryview)):
        raw = bytes(frame)
    else:
        arr = np.asarray(frame)
        if arr.ndim == 2:
            _check_uint16(arr)
            raw = np.ascontiguousarray(arr[:2]).view(np.uint8).tobytes()
        elif arr.ndim == 1:
            if arr.dtype != np.uint8:
                raise TypeError(f"1-D input must be uint8 bytes, got dtype {arr.dtype}")
            raw = arr.tobytes()
        else:
            raise ValueError(f"expected a 2-D frame or a 1-D byte array, got {arr.ndim}-D")
    if len(raw) < HEADER_BYTES:
        raise ValueError(f"frame holds {len(raw)} bytes, need at least {HEADER_BYTES}")
    return raw[:HEADER_BYTES]


def parse_header(frame: np.ndarray) -> FrameHeader:
    """Parse the Telops header of a single raw frame.

    Parameters
    ----------
    frame : numpy.ndarray or bytes-like
        Raw frame with the two header rows still attached: a 2-D uint16
        array of shape ``(H + 2, W)``, a 1-D uint8 array, or a
        bytes-like object of at least 256 bytes.

    Returns
    -------
    FrameHeader
        Decoded header.

    Raises
    ------
    ValueError
        If the signature is not ``b"TC"``, or if the input is too short.
    TypeError
        If a 2-D array is not little-endian uint16.

    Examples
    --------
    >>> frame = cam.grab(strip_header=False)  # doctest: +SKIP
    >>> parse_header(frame).frame_id  # doctest: +SKIP
    1417
    """
    raw = _header_bytes(frame)
    signature = raw[HDR_SIGNATURE : HDR_SIGNATURE + len(SIGNATURE)]
    if signature != SIGNATURE:
        raise ValueError(
            f"bad Telops header signature {signature!r}, expected {SIGNATURE!r}. "
            "Pass a raw frame with the header rows attached (strip_header=False)."
        )

    minor = raw[HDR_XML_MINOR]
    major = raw[HDR_XML_MAJOR]
    version = (major, minor)

    (header_length,) = struct.unpack_from("<H", raw, HDR_HEADER_LENGTH)
    (frame_id,) = struct.unpack_from("<I", raw, HDR_FRAME_ID)
    (data_offset,) = struct.unpack_from("<f", raw, HDR_DATA_OFFSET)
    (data_exp,) = struct.unpack_from("<b", raw, HDR_DATA_EXP)
    (exposure_raw,) = struct.unpack_from("<I", raw, HDR_EXPOSURE_TIME)
    calibration_mode = raw[HDR_CAL_MODE]
    width, height = struct.unpack_from("<HH", raw, HDR_WIDTH)
    (frame_rate_raw,) = struct.unpack_from("<I", raw, HDR_FRAME_RATE)
    (posix_time,) = struct.unpack_from("<I", raw, HDR_POSIX_TIME)
    (subsecond_raw,) = struct.unpack_from("<I", raw, HDR_SUBSECOND)

    buffering_flag: BufferingFlag | int | None = None
    if version >= _BUFFERING_FLAG_SINCE:
        flag_raw = raw[HDR_BUFFERING_FLAG]
        try:
            buffering_flag = BufferingFlag(flag_raw)
        except ValueError:
            buffering_flag = flag_raw

    return FrameHeader(
        header_version=version,
        header_length=header_length,
        frame_id=frame_id,
        data_offset=data_offset,
        data_exp=data_exp,
        exposure_time_us=exposure_raw / 100.0,
        calibration_mode=calibration_mode,
        width=width,
        height=height,
        frame_rate_hz=frame_rate_raw / 1000.0,
        buffering_flag=buffering_flag,
        posix_time=posix_time,
        subsecond_us=subsecond_raw / 10.0,
    )


def parse_headers(frames: np.ndarray) -> list[FrameHeader]:
    """Parse the headers of a stack of raw frames.

    Parameters
    ----------
    frames : numpy.ndarray
        3-D uint16 array of shape ``(N, H + 2, W)`` with the header rows
        still attached. A 2-D single frame is also accepted and gives a
        list of one header.

    Returns
    -------
    list of FrameHeader
        One header per frame, in input order.

    Raises
    ------
    ValueError
        If any frame has a bad signature or is too short.
    TypeError
        If the array is not little-endian uint16.

    Notes
    -----
    This builds one Python object per frame, so it costs a few
    microseconds per frame. For a 40000-frame download use
    :func:`header_timestamps` or :func:`header_frame_ids` instead; they
    read the same bytes with numpy views and return arrays.
    """
    arr = np.asarray(frames)
    if arr.ndim == 2:
        return [parse_header(arr)]
    if arr.ndim != 3:
        raise ValueError(f"expected a 3-D frame stack (N, H + 2, W), got {arr.ndim}-D")
    _check_uint16(arr)
    return [parse_header(arr[i]) for i in range(arr.shape[0])]


def _header_row_bytes(frames: np.ndarray) -> np.ndarray:
    """Return the raw header rows of a frame stack as a uint8 array.

    Parameters
    ----------
    frames : numpy.ndarray
        3-D uint16 stack of shape ``(N, H + 2, W)``, or a single 2-D
        frame, with the header rows attached.

    Returns
    -------
    numpy.ndarray
        uint8 array of shape ``(N, 2 * W * 2)``.

    Raises
    ------
    TypeError
        If the array is not little-endian uint16.
    ValueError
        If the shape is not 2-D or 3-D, or the rows hold fewer than 256
        bytes.
    """
    arr = np.asarray(frames)
    if arr.ndim == 2:
        arr = arr[np.newaxis, ...]
    elif arr.ndim != 3:
        raise ValueError(f"expected a 3-D frame stack (N, H + 2, W), got {arr.ndim}-D")
    _check_uint16(arr)
    n = arr.shape[0]
    row_bytes = 2 * arr.shape[2] * arr.dtype.itemsize
    raw = np.ascontiguousarray(arr[:, :2, :]).view(np.uint8).reshape(n, row_bytes)
    if raw.shape[1] < HEADER_BYTES:
        raise ValueError(
            f"header rows hold {raw.shape[1]} bytes per frame, need at least {HEADER_BYTES}"
        )
    return raw


def header_timestamps(frames: np.ndarray, relative: bool = False) -> np.ndarray:
    """Read the camera timestamps of a frame stack without building objects.

    Parameters
    ----------
    frames : numpy.ndarray
        3-D uint16 stack of shape ``(N, H + 2, W)``, or a single 2-D
        frame, with the header rows attached.
    relative : bool, optional
        If True, subtract the timestamp of the first frame so the result
        starts at 0. Default is False, which gives absolute POSIX
        seconds (UTC).

    Returns
    -------
    numpy.ndarray
        float64 array of shape ``(N,)`` in seconds.

    Raises
    ------
    TypeError
        If the array is not little-endian uint16.
    ValueError
        If the shape is not 2-D or 3-D.

    Notes
    -----
    The signature is not checked here. Use :func:`parse_header` on the
    first frame if you need that validation.

    The sub-second field is a uint32 of 100 ns ticks. Adding it to a
    present-day POSIX second in float64 leaves an absolute resolution of
    about 0.2 us. With ``relative=True`` the whole seconds and the ticks
    are subtracted before they are combined, so the result keeps the
    full 100 ns tick resolution.

    Examples
    --------
    >>> t = header_timestamps(frames, relative=True)  # doctest: +SKIP
    >>> float(np.median(np.diff(t)))  # doctest: +SKIP
    0.001
    """
    raw = _header_row_bytes(frames)
    posix = raw[:, HDR_POSIX_TIME : HDR_POSIX_TIME + 4].copy().view("<u4").ravel()
    # Sub-second field in its native unit: 100 ns ticks (1e-7 s).
    ticks = raw[:, HDR_SUBSECOND : HDR_SUBSECOND + 4].copy().view("<u4").ravel().astype(np.float64)
    if relative and posix.size:
        seconds = (posix.astype(np.int64) - np.int64(posix[0])).astype(np.float64)
        return seconds + (ticks - ticks[0]) * 1e-7
    return posix.astype(np.float64) + ticks * 1e-7


def header_frame_ids(frames: np.ndarray) -> np.ndarray:
    """Read the frame ids of a frame stack without building objects.

    Parameters
    ----------
    frames : numpy.ndarray
        3-D uint16 stack of shape ``(N, H + 2, W)``, or a single 2-D
        frame, with the header rows attached.

    Returns
    -------
    numpy.ndarray
        uint32 array of shape ``(N,)``.

    Raises
    ------
    TypeError
        If the array is not little-endian uint16.
    ValueError
        If the shape is not 2-D or 3-D.

    Examples
    --------
    >>> ids = header_frame_ids(frames)  # doctest: +SKIP
    >>> bool(np.all(np.diff(ids) == 1))  # no dropped frames  # doctest: +SKIP
    True
    """
    raw = _header_row_bytes(frames)
    return raw[:, HDR_FRAME_ID : HDR_FRAME_ID + 4].copy().view("<u4").ravel()
