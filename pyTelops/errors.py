"""Typed errors and result objects for pyTelops downloads."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DownloadStats:
    """Integrity and performance report for one buffer download.

    Populated by :meth:`pyTelops.Camera.buffer_download` and attached to
    :attr:`pyTelops.Camera.last_download_stats`.
    """

    n_frames: int
    n_incomplete: int = 0
    incomplete_frame_ids: list[int] = field(default_factory=list)
    per_frame_missing: dict[int, int] = field(default_factory=dict)
    resend_requested: int = 0
    resend_recovered: int = 0
    resend_failed: int = 0
    recovered_by_retry: int = 0
    first_pass_n_complete: int = 0
    throughput_mbps: float = 0.0
    elapsed_s: float = 0.0
    packet_size_used: int = 1500
    bitrate_used: float = 0.0


class FrameIntegrityError(Exception):
    """Raised when frames remain incomplete after resends and retries.

    Carries the :class:`DownloadStats` so callers can inspect exactly which
    frames were affected.
    """

    def __init__(self, message: str, stats: DownloadStats) -> None:
        super().__init__(message)
        self.stats = stats
