"""Buffer recording started by an external BNC trigger.

Run with::

    python examples/05_external_trigger.py
"""

from __future__ import annotations

import numpy as np

from pyTelops import Camera


def main() -> None:
    with Camera() as cam:
        cam.frame_rate = 2000.0
        cam.configure_trigger(source="external", activation="rising")

        cam.buffer_configure(
            n_sequences=1,
            duration=5.0,
            pre_moi=1000,
            moi_source="external",
        )

        cam.buffer_arm()
        print("Armed, waiting for external trigger...")
        cam.buffer_wait(timeout=60.0)

        data = cam.buffer_download()
        np.save("triggered_data.npy", data)
        print(f"Saved {data.shape} to triggered_data.npy")


if __name__ == "__main__":
    main()
