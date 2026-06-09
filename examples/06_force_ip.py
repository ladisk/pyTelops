"""Re-home a camera that came up on the wrong subnet, using FORCEIP.

When a camera is on no host NIC subnet, discovery reports it with
``reachable == False`` and you cannot connect to it. ``force_ip`` assigns a
new address by MAC so it lands on a reachable subnet.

Run with::

    python examples/06_force_ip.py
"""

from __future__ import annotations

import time

from pyTelops import discover, force_ip


def main() -> None:
    cameras = discover()
    if not cameras:
        print("No cameras found.")
        return

    for cam in cameras:
        state = "reachable" if cam.get("reachable") else "NOT reachable"
        print(f"{cam.get('ip', '?'):18s} mac={cam.get('mac', '?')}  {state}")

    unreachable = [c for c in cameras if not c.get("reachable")]
    if not unreachable:
        print("\nAll cameras are reachable; nothing to re-home.")
        return

    target = unreachable[0]
    # Choose an address on one of your host NIC subnets.
    new_ip = "169.254.10.50"
    new_mask = "255.255.0.0"
    print(f"\nForcing {target.get('mac')} to {new_ip} ...")
    force_ip(target, new_ip, new_mask)

    # The camera reboots its IP stack; wait briefly before re-discovering.
    time.sleep(2.0)
    for cam in discover():
        if cam.get("mac") == target.get("mac"):
            state = "reachable" if cam.get("reachable") else "NOT reachable"
            print(f"Now at {cam.get('ip', '?')}  {state}")


if __name__ == "__main__":
    main()
