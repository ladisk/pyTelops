"""Buffer recording started by an external BNC trigger.

Run with::

    python examples/05_external_trigger.py
"""

from __future__ import annotations

import numpy as np

from pyTelops import Camera, FrameIntegrityError


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

        try:
            data = cam.buffer_download()
        except FrameIntegrityError as exc:
            # Some frames did not arrive intact. Inspect the report, or pass
            # max_dropped_frames=N to buffer_download to tolerate drops.
            print(f"Download incomplete: {exc.stats.n_incomplete} frame(s) bad")
            raise
        np.save("triggered_data.npy", data)
        print(f"Saved {data.shape} to triggered_data.npy")


if __name__ == "__main__":
    main()
