"""External trigger example.

Configure the camera to record to the internal buffer when a BNC
trigger signal arrives.
"""

import numpy as np
from pyTelops import Camera

with Camera() as cam:
    cam.frame_rate = 2000.0

    # --- Configure external trigger ---
    cam.configure_trigger(
        source="external",
        activation="rising",
        enabled=True,
    )

    # --- Buffer recording with external MOI ---
    cam.buffer_configure(
        n_sequences=1,
        duration=5.0,
        pre_moi=1000,           # keep 1000 frames before trigger
        moi_source="external",
    )

    cam.buffer_arm()
    print("Buffer armed, waiting for external trigger...")

    cam.buffer_wait(timeout=60.0)
    print("Recording complete")

    data = cam.buffer_download()
    np.save("triggered_data.npy", data)
    print(f"Saved {data.shape} to triggered_data.npy")
