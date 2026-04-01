"""Buffer recording and download example.

The Telops FAST M3k has a 16GB internal ring buffer that records at
full sensor speed (up to 3100 fps), independent of the Ethernet link.
After recording, frames are downloaded over Ethernet at ~15 fps.
"""

import time
import numpy as np
from pyTelops import Camera
from pyTelops.registers import MemoryBufferMOISource

with Camera() as cam:
    # --- Configure buffer ---
    cam.buffer_configure(
        n_sequences=1,
        frames_per_seq=100,
        pre_moi=20,  # keep 20 frames before trigger
        moi_source=MemoryBufferMOISource.SOFTWARE,
    )
    print(f"Buffer status: {cam.buffer_status().name}")

    # --- Arm and start recording ---
    cam.buffer_arm()
    print("Recording... (camera records to internal memory)")

    # Simulate waiting for an event
    time.sleep(1.0)

    # --- Fire MOI trigger ---
    cam.buffer_fire_moi()
    print("MOI fired, waiting for recording to finish...")

    # Wait for recording to complete
    time.sleep(1.0)

    # --- Check what was recorded ---
    n_recorded = cam.buffer_recorded_frames(sequence=0)
    print(f"Recorded {n_recorded} frames")

    # --- Download ---
    data = cam.buffer_download(sequence=0)
    if data is not None:
        print(f"Downloaded: {data.shape}, {data.dtype}")
        np.save("buffer_data.npy", data)
        print("Saved to buffer_data.npy")

    # --- Clean up ---
    cam.buffer_clear()
