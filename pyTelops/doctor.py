"""Environment and camera diagnostics behind ``pytelops doctor``.

Each check returns a dict ``{name, status, detail, fix}`` where *status* is
``"ok"``, ``"warn"``, ``"fail"`` or ``"skip"``. The camera probe is read-only:
it reads registers over GVCP without taking the control privilege. Only the
optional stream test connects to the camera and changes its state.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import socket
import struct
import sys
import time
from collections.abc import Callable
from pathlib import Path

from pyGigEVision import GVCPClient, GVCPError
from pyGigEVision.standard import (
    REG_CCP,
    REG_SC_DEST_ADDR,
    REG_SC_HOST_PORT,
    REG_SC_PACKET_DELAY,
    REG_SC_PACKET_SIZE,
)

from . import registers as reg
from .camera import Camera, _find_local_ip_for, discover
from .connection import _host_interfaces, _link_local_warning

RMEM_MIN = 16777216
GVCP_ACCESS_DENIED = 0x8006

_USB_NAMES = {"0b95:1790": "ASIX", "2357:0601": "TP-Link"}

_TDC_FLAGS = {
    getattr(reg, n): n[4:].lower()
    for n in dir(reg)
    if n.startswith("TDC_") and isinstance(getattr(reg, n), int)
}

_DEFAULTS_NOTE = (
    "connect applied pyTelops defaults (bad-pixel replacement on, fixed frame-rate mode)"
)


def _chk(name, status, detail="", fix=""):
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def _safe(name, fn, *args, **kwargs):
    """Run a check; turn any exception into a ``fail`` result."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        return _chk(name, "fail", f"check crashed: {type(e).__name__}: {e}")


def _is_linux():
    return sys.platform.startswith("linux")


def _read(path):
    return Path(path).read_text().strip()


def _host_ipv4():
    """Return ``{interface: [ipv4, ...]}`` using psutil (best effort)."""
    out: dict[str, list[str]] = {}
    for name, ip in _host_interfaces():
        out.setdefault(name, []).append(ip)
    return out


def decode_tdc(value):
    """Return the names of the set TDC status bits."""
    return [name for bit, name in sorted(_TDC_FLAGS.items()) if value & bit]


# ---------------------------------------------------------------- host checks


def _usb_device_of(iface_dir, sysfs_root):
    """Walk up from the interface device dir to the USB device dir.

    Returns ``(vid, pid, port)`` or ``None`` when the NIC is not USB.
    """
    dev = iface_dir / "device"
    if not dev.exists():
        return None
    cur = dev.resolve()
    root = Path(sysfs_root).resolve()
    while cur != cur.parent:
        if (cur / "idVendor").exists() and (cur / "idProduct").exists():
            return _read(cur / "idVendor"), _read(cur / "idProduct"), cur.name
        if cur == root:
            break
        cur = cur.parent
    return None


def check_adapters(sysfs_root="/sys", host_ipv4=_host_ipv4):
    if not _is_linux():
        return [_chk("adapters", "skip", "Linux only")]
    net = Path(sysfs_root) / "class" / "net"
    addrs = host_ipv4()
    results = []
    names = sorted(p.name for p in net.iterdir()) if net.is_dir() else []
    for name in names:
        if name == "lo":
            continue
        d = net / name
        try:
            carrier = _read(d / "carrier") == "1"
        except OSError:
            carrier = False
        link_local = any(a.startswith("169.254.") for a in addrs.get(name, []))
        if not (carrier or link_local):
            continue
        try:
            mtu = _read(d / "mtu")
        except OSError:
            mtu = "?"
        driver = "?"
        with contextlib.suppress(OSError):
            driver = os.path.basename(os.readlink(d / "device" / "driver"))
        usb = _usb_device_of(d, sysfs_root)
        label = ""
        status, fix = "ok", ""
        if usb:
            vid, pid, port = usb
            key = f"{vid}:{pid}"
            label = f"{_USB_NAMES.get(key, 'USB')} {key} "
            if key == "2357:0601":
                status = "warn"
                fix = "do not use this adapter (it drops most stream packets); use another one"
            elif key == "0b95:1790" and driver == "cdc_ncm":
                status = "warn"
                fix = (
                    f"echo 1 | sudo tee /sys/bus/usb/devices/{port}/bConfigurationValue, "
                    "or install the udev rule (troubleshooting: Linux USB Ethernet adapters)"
                )
        detail = f"{label}on {driver}, carrier {'up' if carrier else 'down'}, mtu {mtu}" + (
            ", link-local" if link_local else ""
        )
        results.append(_chk(f"adapter {name}", status, detail, fix))
    if not results:
        results.append(
            _chk(
                "adapters",
                "warn",
                "no interface with a link-local address or carrier",
                "connect the camera directly with a cable and check the adapter",
            )
        )
    warning = _link_local_warning([(n, ip) for n, ips in addrs.items() for ip in ips])
    if warning:
        results.append(_chk("link-local", "warn", warning))
    return results


def check_rmem(path="/proc/sys/net/core/rmem_max"):
    if not _is_linux():
        return _chk("rmem_max", "skip", "Linux only")
    value = int(_read(path))
    if value < RMEM_MIN:
        return _chk(
            "rmem_max",
            "warn",
            f"net.core.rmem_max is {value}, below {RMEM_MIN}",
            f"sudo sysctl -w net.core.rmem_max={RMEM_MIN}",
        )
    return _chk("rmem_max", "ok", str(value))


def check_route(ctx):
    cam_ip, iface = ctx.get("ip"), ctx.get("interface_ip")
    if not cam_ip or not iface:
        return _chk("route", "skip", "no discovered camera interface")
    routed = _find_local_ip_for(cam_ip)
    if routed != iface:
        return _chk(
            "route",
            "warn",
            f"camera replied on {iface} but the OS routes {cam_ip} via {routed}",
            "another interface (for example a VPN) captures the route; "
            "stop the VPN or pass local_ip to Camera",
        )
    return _chk("route", "ok", f"{cam_ip} via {iface}")


def check_arp(ctx, arp_path="/proc/net/arp"):
    if not _is_linux():
        return _chk("arp", "skip", "Linux only")
    cam_ip = ctx.get("ip")
    if not cam_ip:
        return _chk("arp", "skip", "no camera IP")
    entry = None
    for line in Path(arp_path).read_text().splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6 and parts[0] == cam_ip:
            entry = parts
    iface = entry[5] if entry else None
    if not iface:
        for name, ips in _host_ipv4().items():
            if ctx.get("interface_ip") in ips:
                iface = name
    fix = (
        f"the ASIX vendor driver needs promiscuous mode: sudo ip link set {iface or '<iface>'} "
        "promisc on, or install the udev rule (troubleshooting: Linux USB Ethernet adapters)"
    )
    if entry is None:
        return _chk("arp", "warn", f"no ARP entry for {cam_ip}", fix)
    if int(entry[2], 16) == 0:
        return _chk("arp", "warn", f"ARP entry for {cam_ip} is incomplete (flags 0x0)", fix)
    return _chk("arp", "ok", f"{cam_ip} at {entry[3]} on {entry[5]}")


# -------------------------------------------------------------- camera checks


def check_discovery(ctx, ip=None):
    cams = discover()
    if ip:
        match = [c for c in cams if c.get("ip") == ip]
        ctx["ip"] = ip
    else:
        match = cams
    if not match:
        if ip:
            return _chk(
                "discovery",
                "fail",
                f"{ip} did not answer the discovery broadcast; probing it directly",
                "check the cable and the VPN, power-cycle the camera (its IP changes after a power cycle)",
            )
        return _chk(
            "discovery",
            "fail",
            "no Telops camera found",
            "use a direct cable, not a wall socket; stop any VPN; if the link is up and "
            "nothing is found, power-cycle the camera (its IP changes after a power cycle)",
        )
    cam = match[0]
    ctx.update(
        ip=cam.get("ip"),
        mac=cam.get("mac"),
        model=cam.get("model"),
        serial=cam.get("serial"),
        firmware=cam.get("device_version"),
        interface_ip=cam.get("interface_ip") or None,
    )
    detail = f"{cam.get('model', '?')} serial {cam.get('serial', '?')} at {cam.get('ip')}"
    if len(match) > 1 and not ip:
        detail += f" (first of {len(match)} cameras; pass --ip to choose)"
    if cam.get("reachable") is False:
        return _chk(
            "discovery",
            "warn",
            detail + ", but its subnet is not reachable from any host NIC",
            "assign a matching address with force_ip (GVCPClient.force_ip) or change the host address",
        )
    return _chk("discovery", "ok", detail)


def _open_probe(cam_ip, local_ip):
    """Return a read-only GVCPClient (no CCP write, no heartbeat).

    ``connect(control=False)`` only opens the socket. Reads need no
    privilege, so ``read_reg`` is used directly.
    """
    client = GVCPClient(cam_ip, local_ip or None, 1.0, n_retries=2, cmd_timeout=0.4)
    client.connect(control=False)
    return client


def _int_to_ip(value):
    return socket.inet_ntoa(struct.pack(">I", value & 0xFFFFFFFF))


def check_probe(ctx, stream_requested=False):
    cam_ip = ctx.get("ip")
    if not cam_ip:
        return _chk("probe", "skip", "no camera IP")
    local_ip = ctx.get("interface_ip")
    if not local_ip:
        with contextlib.suppress(OSError):
            local_ip = _find_local_ip_for(cam_ip)
    client = _open_probe(cam_ip, local_ip)
    regs = {}
    try:
        try:
            regs["tdc"] = client.read_reg(reg.REG_TDC_STATUS)
        except GVCPError as e:
            if e.status == 0:
                return _chk(
                    "probe",
                    "fail",
                    f"GVCP timeout reading {cam_ip}",
                    "the camera is hung or unreachable; power-cycle the camera",
                )
            return _chk("probe", "fail", f"GVCP error: {e}")
        for key, addr in (
            ("not_ready", reg.REG_DEVICE_NOT_READY),
            ("dest", REG_SC_DEST_ADDR),
            ("port", REG_SC_HOST_PORT),
            ("packet_size", REG_SC_PACKET_SIZE),
            ("ccp", REG_CCP),
            ("trigger", reg.REG_TRIGGER_MODE),
            ("buffer", reg.REG_MEMORY_BUFFER_MODE),
        ):
            try:
                regs[key] = client.read_reg(addr)
            except GVCPError:
                regs[key] = None
    finally:
        with contextlib.suppress(Exception):
            client.disconnect()
    ctx["regs"] = regs

    flags = decode_tdc(regs["tdc"])
    acquiring = bool(regs["tdc"] & reg.TDC_ACQUISITION_STARTED)
    ready = not regs["not_ready"] if regs["not_ready"] is not None else None
    ctx.update(
        tdc_flags=flags,
        tdc_status=regs["tdc"],
        acquiring=acquiring,
        ready=ready,
        stream_port=regs["port"],
    )
    dest = _int_to_ip(regs["dest"]) if regs["dest"] is not None else None
    ctx["stream_dest"] = dest
    ctx["controlled"] = bool(regs["ccp"]) if regs["ccp"] is not None else None

    problems, fixes = [], []
    if regs["ccp"]:
        problems.append("controlled by another application")
        fixes.append("close the other application, or wait about 15 s if it crashed")
    if acquiring and regs["port"] and not stream_requested:
        own = {ip for ips in _host_ipv4().values() for ip in ips}
        msg = f"camera acquiring; streaming to {dest}:{regs['port']}"
        if dest not in own:
            msg += " (not an address of this host)"
        problems.append(msg)
        fixes.append("stop the application that started the stream, or reconnect to take over")
    if ready is False:
        waiting = [f for f in flags if f != "acquisition_started"]
        problems.append(f"not ready, waiting for: {', '.join(waiting) or 'unknown'}")
        fixes.append("wait for the camera (cooling takes minutes) and run doctor again")
    state = f"tdc 0x{regs['tdc']:04X} [{', '.join(flags) or 'idle'}]"
    if problems:
        return _chk("probe", "warn", "; ".join(problems) + f"; {state}", "; ".join(fixes))
    return _chk("probe", "ok", f"{state}, ready, not acquiring")


def check_stream(ctx, frames=20):
    regs = ctx.get("regs")
    if regs is None:
        return _chk("stream", "skip", "probe did not read the camera")
    if regs.get("trigger"):
        return _chk("stream", "skip", "trigger mode is on; free-run test not possible")
    if regs.get("buffer"):
        return _chk("stream", "skip", "memory buffer mode is not off")

    cam = Camera(ip=ctx["ip"], local_ip=ctx.get("interface_ip") or None)
    saved = {}
    received = incomplete = dropped = 0
    stamps = []
    try:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                cam.connect(timeout=5, cooling_timeout=5)
        except TimeoutError as e:
            return _chk("stream", "warn", f"camera not ready: {e}", "wait until cooling finishes")
        except GVCPError as e:
            if e.status == GVCP_ACCESS_DENIED:
                return _chk(
                    "stream",
                    "warn",
                    "another application holds control",
                    "close the other application and run doctor again",
                )
            raise
        with contextlib.suppress(Exception):
            ctx["temperature_c"] = cam.sensor_temperature("sensor")
        saved = {
            REG_SC_PACKET_SIZE: cam._gvcp.read_reg(REG_SC_PACKET_SIZE),
            REG_SC_PACKET_DELAY: cam._gvcp.read_reg(REG_SC_PACKET_DELAY),
        }
        try:
            rate = float(cam.frame_rate)
        except Exception:  # noqa: BLE001
            rate = 0.0
        timeout = max(2.0, 3.0 / rate) if rate > 0 else 2.0
        cam.acquisition_start()
        prev = None
        for _ in range(frames):
            result = cam._gvsp.get_frame_with_info(timeout=timeout)
            if result is None:
                break
            _, info = result
            received += 1
            stamps.append(time.monotonic())
            if not info.get("complete", True):
                incomplete += 1
            bid = info.get("block_id")
            if prev is not None and bid is not None and bid > prev + 1:
                dropped += bid - prev - 1
            if bid is not None:
                prev = bid
    except Exception as e:  # noqa: BLE001
        return _chk("stream", "fail", f"{type(e).__name__}: {e}")
    finally:
        with contextlib.suppress(Exception):
            cam.acquisition_stop()
        for addr, value in saved.items():
            with contextlib.suppress(Exception):
                cam._gvcp.write_reg(addr, value)
        with contextlib.suppress(Exception):
            cam.disconnect()

    fps = None
    if len(stamps) > 1 and stamps[-1] > stamps[0]:
        fps = (len(stamps) - 1) / (stamps[-1] - stamps[0])
    ctx["stream_fps"] = fps
    detail = (
        f"{received}/{frames} frames, {incomplete} incomplete, {dropped} dropped"
        + (f", {fps:.1f} fps" if fps else "")
        + f"; {_DEFAULTS_NOTE}"
    )
    if received == 0:
        return _chk(
            "stream",
            "fail",
            f"0 frames while acquiring; {_DEFAULTS_NOTE}",
            "dead stream channel: check the adapter and the rmem_max setting, then power-cycle the camera",
        )
    if incomplete or dropped or received < frames:
        return _chk(
            "stream",
            "warn",
            detail,
            "packet loss: check the adapter, cable and rmem_max; see the troubleshooting page",
        )
    return _chk("stream", "ok", detail)


# --------------------------------------------------------------------- driver


@contextlib.contextmanager
def _quiet():
    """Silence stdout and pyTelops/pyGigEVision logging below WARNING."""
    loggers = [logging.getLogger(n) for n in ("pyTelops", "pyGigEVision")]
    old = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(max(lg.level, logging.WARNING))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            yield
    finally:
        for lg, lvl in zip(loggers, old, strict=True):
            lg.setLevel(lvl)


def run_doctor(
    ip=None,
    stream=False,
    frames=20,
    sysfs_root="/sys",
    rmem_path="/proc/sys/net/core/rmem_max",
    arp_path="/proc/net/arp",
):
    """Run all checks. Returns ``(checks, camera)``."""
    ctx: dict = {}
    checks: list[dict] = []

    def add(res):
        checks.extend(res if isinstance(res, list) else [res])

    steps: list[tuple[str, Callable]] = [
        ("adapters", lambda: check_adapters(sysfs_root)),
        ("rmem_max", lambda: check_rmem(rmem_path)),
        ("discovery", lambda: check_discovery(ctx, ip)),
        ("route", lambda: check_route(ctx)),
        ("probe", lambda: check_probe(ctx, stream)),
        ("arp", lambda: check_arp(ctx, arp_path)),
    ]
    with _quiet():
        for name, fn in steps:
            add(_safe(name, fn))
        if stream:
            add(_safe("stream", check_stream, ctx, frames))
        else:
            add(_chk("stream", "skip", "not requested; add --stream to test frame delivery"))

    camera = {
        k: ctx.get(k)
        for k in (
            "ip",
            "mac",
            "model",
            "serial",
            "firmware",
            "interface_ip",
            "tdc_flags",
            "acquiring",
            "ready",
            "temperature_c",
            "stream_dest",
            "stream_port",
            "controlled",
        )
    }
    return checks, camera


def summarize(checks):
    return {s: sum(1 for c in checks if c["status"] == s) for s in ("ok", "warn", "fail", "skip")}


def exit_code(checks):
    statuses = {c["status"] for c in checks}
    return 2 if "fail" in statuses else 1 if "warn" in statuses else 0


def report_dict(checks, camera):
    return {
        "summary": summarize(checks),
        "problems": [c for c in checks if c["status"] in ("warn", "fail")],
        "camera": camera,
    }


def format_line(c):
    line = f"[{c['status'].upper()}] {c['name']}: {c['detail']}"
    if c["fix"]:
        line += f" -> fix: {c['fix']}"
    return line
