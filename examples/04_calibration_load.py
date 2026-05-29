"""Load a calibration collection by lens and target temperature.

The calibration info comes from the USB drive shipped with the camera. Point
``CAL_PATH`` at that folder before running.

Run with::

    python examples/04_calibration_load.py
"""

from __future__ import annotations

from pyTelops import Camera

CAL_PATH = "path/to/TEL-8050 Calibration Data"


def main() -> None:
    with Camera() as cam:
        cam.load_calibration_info(CAL_PATH)

        for c in cam.calibration_collections():
            print(c["index"], c["lens"], c.get("temp_range"))

        cam.calibration_load(lens="50mm", temp=25)
        print("Active:", cam.calibration_active())


if __name__ == "__main__":
    main()
