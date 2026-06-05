"""Connection diagnostics and download tuning for Telops cameras."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


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


def tune_connection(
    cam,
    probe_only=False,
    candidate_packet_sizes=None,
    candidate_bitrates=None,
    socket_buffers=None,
    test_frames=300,
    read_nic_info=False,
):
    """Probe the link and sweep download settings to find a stable+fast config.

    Phase 1 probes the path (read-only). With *probe_only*, returns after the
    probe. Phase 2 runs real downloads across candidate settings and ranks them
    stability-first. No system or camera settings are persisted or mutated.

    Parameters
    ----------
    cam : Camera
        A connected Telops camera with frames already in its memory buffer
        (record a sequence before sweeping).
    probe_only : bool, optional
        Stop after the read-only probe; run no downloads. Default ``False``.
    candidate_packet_sizes, candidate_bitrates, socket_buffers : list or None
        Values to sweep. ``None`` picks sensible defaults (packet sizes capped
        at the probed path maximum).
    test_frames : int, optional
        Frames per trial download. Default 300.
    read_nic_info : bool, optional
        Read (never change) host NIC facts to annotate the report. Default
        ``False``. Requires ``psutil``.

    Returns
    -------
    ConnectionReport
    """
    warnings = list(_preflight_warnings(cam))

    probe = ProbeInfo()
    try:
        probe.max_packet_size = cam._probe_max_packet_size(16260)
    except Exception:  # noqa: BLE001 - probe is best-effort
        probe.max_packet_size = None
    if read_nic_info:
        _read_nic_info(cam, probe, warnings)

    if probe_only:
        return ConnectionReport(recommended={}, sweep=[], probe=probe, warnings=warnings)

    if candidate_packet_sizes is None:
        ceiling = probe.max_packet_size or 1500
        candidate_packet_sizes = sorted({s for s in (1500, 4000, 8000, 16260) if s <= ceiling})
    if candidate_bitrates is None:
        candidate_bitrates = [1000, 700, 500, 300]
    if socket_buffers is None:
        socket_buffers = [0]

    if any(sb for sb in socket_buffers):
        warnings.append(
            "socket_buffers values are recorded but not yet applied "
            "(SO_RCVBUF tuning is not wired into buffer_download); "
            "sweeping multiple values produces trials that differ only by label."
        )

    trials = []
    for ps in candidate_packet_sizes:
        for br in candidate_bitrates:
            for sb in socket_buffers:
                try:
                    cam.buffer_download(
                        n_frames=test_frames,
                        packet_size=ps,
                        bitrate_mbps=br,
                        convert=False,
                        strip_header=False,
                        verbose=False,
                        max_dropped_frames=test_frames,
                        retries=0,
                    )
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"trial ps={ps} br={br} failed: {exc}")
                    continue
                s = cam.last_download_stats
                if s is None:
                    continue
                pct = 100.0 * s.n_incomplete / max(1, test_frames)
                trials.append(
                    TrialResult(
                        packet_size=ps,
                        bitrate_mbps=br,
                        socket_buffer=sb,
                        throughput_mbps=s.throughput_mbps,
                        pct_incomplete=pct,
                    )
                )

    return ConnectionReport.from_trials(trials, probe=probe, warnings=warnings)


def _preflight_warnings(cam):
    """Yield human-readable warnings for known environmental gotchas."""
    try:
        import socket

        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("169.254."):
                yield (
                    "A link-local (169.254.x.x) adapter is present; if discovery "
                    "or downloads misbehave, a VPN adapter (e.g. Tailscale) may be "
                    "hijacking the route - stop it during acquisition."
                )
                break
    except Exception:  # noqa: BLE001
        pass


def _read_nic_info(cam, probe, warnings):
    """Read-only NIC facts. Never changes system state. Best-effort."""
    try:
        import psutil
    except Exception:  # noqa: BLE001
        warnings.append("read_nic_info=True but psutil is not installed; skipping NIC facts.")
        return
    try:
        stats = psutil.net_if_stats()
        for name, st in stats.items():
            if st.isup and getattr(st, "speed", 0):
                probe.adapter_name = name
                probe.link_speed_mbps = st.speed
                low = name.lower()
                probe.is_usb_adapter = any(k in low for k in ("usb", "ax88", "rtl8153", "hub"))
                if probe.is_usb_adapter:
                    warnings.append(
                        f"NIC '{name}' looks like a USB adapter; issue #8 documents "
                        f"~half throughput and rare unrecoverable drops on these."
                    )
                break
    except Exception:  # noqa: BLE001
        pass
