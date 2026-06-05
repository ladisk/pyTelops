"""Connection diagnostics and download tuning for Telops cameras."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TrialResult:
    """One measured (packet_size, bitrate, socket_buffer) configuration."""

    packet_size: int
    bitrate_mbps: float
    socket_buffer: int
    throughput_mbps: float
    pct_incomplete: float


@dataclass
class ProbeInfo:
    """Read-only facts gathered before/without running downloads."""

    max_packet_size: int | None = None
    link_speed_mbps: int | None = None
    adapter_name: str | None = None
    is_usb_adapter: bool | None = None
    so_rcvbuf_max: int | None = None


@dataclass
class ConnectionReport:
    """Result of :func:`tune_connection`."""

    recommended: dict
    sweep: list[TrialResult] = field(default_factory=list)
    probe: ProbeInfo | None = None
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_trials(cls, trials, probe, warnings):
        ranked = _rank_trials(trials)
        best = ranked[0] if ranked else None
        recommended = (
            {
                "packet_size": best.packet_size,
                "bitrate_mbps": best.bitrate_mbps,
                "socket_buffer": best.socket_buffer,
            }
            if best is not None
            else {}
        )
        return cls(recommended=recommended, sweep=ranked, probe=probe, warnings=list(warnings))

    def apply(self, cam):
        """Store the recommended kwargs on *cam* for later buffer_download calls.

        Does not change any system or camera setting; just records the
        recommendation as ``cam.recommended_download_kwargs``.
        """
        cam.recommended_download_kwargs = dict(self.recommended)
        return self.recommended


def _rank_trials(trials):
    """Rank stability-first: zero-drop beats lossy; ties broken by throughput."""
    return sorted(
        trials,
        key=lambda t: (t.pct_incomplete > 0, t.pct_incomplete, -t.throughput_mbps),
    )
