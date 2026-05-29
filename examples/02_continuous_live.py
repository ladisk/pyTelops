"""Continuous acquisition for a live display.

Uses ``read_frame(latest=True)`` so the displayed frame never lags behind
real time when the draw loop is slower than the camera frame rate.

Run with::

    python examples/02_continuous_live.py
"""

from __future__ import annotations

import time

from pyTelops import Camera


def main() -> None:
    with Camera() as cam:
        cam.calibration_mode = "RT"
        cam.frame_rate = 30.0
        cam.acquisition_start()
        try:
            t_end = time.monotonic() + 5.0
            while time.monotonic() < t_end:
                frame = cam.read_frame(timeout=2.0, latest=True)
                if frame is not None:
                    print(f"latest frame mean: {frame.mean():.2f} C")
        finally:
            cam.acquisition_stop()


if __name__ == "__main__":
    main()
