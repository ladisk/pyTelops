"""Connect to a Telops camera and grab one calibrated frame.

Run with::

    python examples/01_connect_and_grab.py
"""

from __future__ import annotations

from pyTelops import Camera, discover


def main() -> None:
    for cam in discover():
        print(f"{cam['manufacturer']} {cam['model']} at {cam['ip']}")

    with Camera() as cam:
        cam.calibration_mode = "RT"
        cam.integration_time_auto = "continuous"

        frame = cam.grab()
        print(f"Frame: {frame.shape}, {frame.dtype}")

        frames = cam.acquire(10)
        print(f"Batch: {frames.shape}")


if __name__ == "__main__":
    main()
