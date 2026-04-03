"""
Command-line interface for pyTelops.

Usage:
    pytelops discover     Find cameras on the network
    pytelops info         Show camera configuration
    pytelops grab         Grab a single frame and save to file
    pytelops live         Open live viewer (requires gui extra)
    pytelops setup        Configure OS (firewall rules, MTU)
"""

import argparse
import json
import sys
from typing import Optional


def cmd_discover(args):
    """Find cameras on the network."""
    from .camera import discover

    cameras = discover(timeout=args.timeout)
    if not cameras:
        print("No cameras found.")
        return 1

    for i, cam in enumerate(cameras):
        print(f"\n[{i}] {cam.get('manufacturer', '?')} "
              f"{cam.get('model', '?')}")
        print(f"    IP:      {cam['ip']}")
        print(f"    Serial:  {cam.get('serial', '?')}")
        print(f"    Version: {cam.get('device_version', '?')}")

    print(f"\nFound {len(cameras)} camera(s)")
    return 0


def cmd_info(args):
    """Show camera configuration."""
    from .camera import Camera

    with Camera(ip=args.ip) as cam:
        info = cam.info
        for key, val in info.items():
            print(f"  {key:20s}: {val}")
    return 0


def cmd_grab(args):
    """Grab a single frame and save."""
    import numpy as np
    from .camera import Camera

    with Camera(ip=args.ip) as cam:
        if args.integration_time:
            cam.integration_time = args.integration_time
        frame = cam.grab(timeout=args.timeout)

    if frame is None:
        print("Failed to grab frame")
        return 1

    output = args.output or "frame.npy"
    if output.endswith(".npy"):
        np.save(output, frame)
    elif output.endswith(".csv"):
        fmt = "%d" if frame.dtype == np.uint16 else "%.4f"
        np.savetxt(output, frame, delimiter=",", fmt=fmt)
    else:
        np.save(output + ".npy", frame)
        output += ".npy"

    print(f"Saved {frame.shape} {frame.dtype} to {output}")
    return 0


def cmd_live(args):
    """Open live viewer."""
    from .camera import Camera

    with Camera(ip=args.ip) as cam:
        cam.live_view(colormap=args.colormap, scale=args.scale)
    return 0


def cmd_setup(args):
    """Configure OS for GigE Vision camera use."""
    import os
    import platform

    system = platform.system()

    if system == "Windows":
        print("Windows GigE Vision setup")
        print("=" * 40)
        print()
        print("The following may need admin privileges.")
        print()

        python_exe = sys.executable
        print(f"Python: {python_exe}")
        print()

        # Check firewall rule
        print("1. Firewall rule for GVSP (inbound UDP):")
        print(f'   netsh advfirewall firewall add rule '
              f'name="pyTelops-GVSP" dir=in action=allow '
              f'protocol=UDP program="{python_exe}"')
        print()

        # Check network profile
        print("2. Set camera network adapter to Private profile:")
        print("   (Replace 'Ethernet 5' with your adapter name)")
        print("   Set-NetConnectionProfile -InterfaceAlias 'Ethernet 5' "
              "-NetworkCategory Private")
        print()

        # Jumbo frames
        print("3. (Optional) Enable jumbo frames for higher throughput:")
        print("   Set MTU to 9000 in adapter properties > Advanced")
        print()

    elif system == "Linux":
        print("Linux GigE Vision setup")
        print("=" * 40)
        print()

        print("1. Increase UDP receive buffer:")
        print("   sudo sysctl -w net.core.rmem_max=16777216")
        print("   sudo sysctl -w net.core.rmem_default=16777216")
        print()

        print("2. (Optional) Enable jumbo frames:")
        print("   sudo ip link set eth0 mtu 9000")
        print()

        print("3. Firewall (if active):")
        print("   sudo ufw allow in proto udp to any port 3956")
        print("   sudo ufw allow in proto udp from 169.254.0.0/16")
        print()
    else:
        print(f"No setup instructions for {system}")
        return 1

    return 0


def main(argv: Optional[list[str]] = None):
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        prog="pytelops",
        description="pyTelops — Telops thermal camera driver")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {_get_version()}")

    sub = parser.add_subparsers(dest="command")

    # discover
    p_disc = sub.add_parser("discover", help="Find cameras on the network")
    p_disc.add_argument("--timeout", type=float, default=2.0)

    # info
    p_info = sub.add_parser("info", help="Show camera configuration")
    p_info.add_argument("--ip", default=None, help="Camera IP (auto-discover)")

    # grab
    p_grab = sub.add_parser("grab", help="Grab a single frame")
    p_grab.add_argument("-o", "--output", default="frame.npy",
                        help="Output file (.npy or .csv)")
    p_grab.add_argument("--ip", default=None)
    p_grab.add_argument("--integration-time", type=float, default=None,
                        help="Integration time in microseconds")
    p_grab.add_argument("--timeout", type=float, default=5.0)

    # live
    p_live = sub.add_parser("live", help="Open live viewer")
    p_live.add_argument("--ip", default=None)
    p_live.add_argument("--colormap", default="inferno",
                        help="Colormap name")
    p_live.add_argument("--scale", type=int, default=2,
                        help="Display scale factor")

    # setup
    p_setup = sub.add_parser("setup",
                             help="Configure OS for GigE Vision")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    commands = {
        "discover": cmd_discover,
        "info": cmd_info,
        "grab": cmd_grab,
        "live": cmd_live,
        "setup": cmd_setup,
    }

    return commands[args.command](args)


def _get_version() -> str:
    try:
        from . import __version__
        return __version__
    except ImportError:
        return "unknown"


if __name__ == "__main__":
    sys.exit(main())
