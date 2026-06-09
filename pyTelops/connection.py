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


_VPN_NAME_KEYS = ("tailscale", "zerotier", "wireguard", "vpn", "wg")
_USB_ADAPTER_KEYS = ("usb", "ax88", "asix", "rtl8153", "rtl8156", "hub")


def _link_local_warning(interfaces):
    """Return a warning when the camera's link-local route is ambiguous.

    Parameters
    ----------
    interfaces : list of (str, str)
        ``(name, ipv4)`` for each host interface.

    Notes
    -----
    A direct camera link is itself link-local (169.254.x.x), so the presence of
    a single link-local adapter is normal and is NOT flagged. A warning is
    returned only when the route is genuinely ambiguous: more than one
    link-local adapter is up, or a VPN-like adapter is present alongside one.
    Returns ``None`` otherwise.
    """
    link_local = [(n, ip) for n, ip in interfaces if ip.startswith("169.254.")]
    if not link_local:
        return None
    vpn = [n for n, _ in interfaces if any(k in n.lower() for k in _VPN_NAME_KEYS)]
    if len(link_local) > 1 or vpn:
        names = ", ".join(sorted({n for n, _ in link_local} | set(vpn)))
        return (
            f"Multiple link-local / VPN adapters are up ({names}); a VPN adapter "
            f"can take over the camera route. If discovery or downloads misbehave, "
            f"stop it or pass an explicit local_ip for the camera NIC."
        )
    return None


def _is_usb_adapter(name, description=""):
    """Return ``True`` if a NIC looks like a USB-to-Ethernet adapter.

    Checks the connection *name* and the adapter *description* (the latter is
    where Windows records "ASIX USB to Gigabit Ethernet"; the connection name is
    just "Ethernet 7").
    """
    text = f"{name} {description or ''}".lower()
    return any(k in text for k in _USB_ADAPTER_KEYS)


def _host_interfaces():
    """Return ``[(name, ipv4)]`` for host interfaces (best-effort, psutil)."""
    try:
        import socket

        import psutil

        out = []
        for name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET:
                    out.append((name, a.address))
        return out
    except Exception:  # noqa: BLE001
        return []


def _adapter_descriptions():
    """Return ``{connection_name: interface_description}`` on Windows.

    psutil keys interfaces by connection name ("Ethernet 7"), not the adapter
    description ("ASIX USB to Gigabit Ethernet"), so USB detection needs this
    extra lookup. Best-effort: returns ``{}`` on non-Windows or any failure.
    """
    import sys

    if not sys.platform.startswith("win"):
        return {}
    try:
        import json
        import subprocess

        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-NetAdapter | Select-Object Name,InterfaceDescription | ConvertTo-Json",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        data = json.loads(proc.stdout or "[]")
        if isinstance(data, dict):
            data = [data]
        return {d.get("Name", ""): d.get("InterfaceDescription", "") for d in data}
    except Exception:  # noqa: BLE001
        return {}


def _preflight_warnings(cam):
    """Yield human-readable warnings for common environment problems."""
    warning = _link_local_warning(_host_interfaces())
    if warning:
        yield warning


def _read_nic_info(cam, probe, warnings):
    """Read-only NIC facts. Never changes system state. Best-effort.

    Targets the NIC carrying the camera's ``local_ip`` (so the USB/speed facts
    describe the camera link, not an unrelated WiFi adapter), and detects USB
    adapters by their description, which is where Windows records the vendor.
    """
    try:
        import socket

        import psutil
    except Exception:  # noqa: BLE001
        warnings.append("read_nic_info=True but psutil is not installed; skipping NIC facts.")
        return
    try:
        local_ip = getattr(cam, "_local_ip", "") or ""
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        descriptions = _adapter_descriptions()

        # Prefer the NIC carrying the camera's local_ip; else the first up NIC
        # that reports a link speed.
        target = None
        for name, alist in addrs.items():
            if any(a.family == socket.AF_INET and a.address == local_ip for a in alist):
                target = name
                break
        if target is None:
            for name, st in stats.items():
                if st.isup and getattr(st, "speed", 0):
                    target = name
                    break
        if target is None:
            return

        st = stats.get(target)
        desc = descriptions.get(target, "")
        probe.adapter_name = target
        probe.link_speed_mbps = getattr(st, "speed", None) if st else None
        probe.is_usb_adapter = _is_usb_adapter(target, desc)
        if probe.is_usb_adapter:
            label = f"NIC '{target}'" + (f" ({desc})" if desc else "")
            warnings.append(
                f"{label} looks like a USB adapter; issue #8 documents ~half "
                f"throughput and rare unrecoverable drops on these."
            )
    except Exception:  # noqa: BLE001
        pass
