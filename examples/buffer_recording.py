"""Buffer recording and download example.

The Telops FAST M3k has a 16GB internal ring buffer that records at
full sensor speed (up to 3100 fps at full frame), independent of the
Ethernet link. After recording, frames are downloaded over Ethernet.
"""

import numpy as np
from pyTelops import Camera

with Camera() as cam:
    cam.frame_rate = 2000.0
    cam.integration_time_auto = "continuous"

    # --- Configure buffer: 1 sequence, 5 seconds ---
    cam.buffer_configure(
        n_sequences=1,
        duration=5.0,           # uses current frame_rate
        moi_source="software",
    )
    print(cam.buffer_info())

    # --- Record ---
    cam.buffer_record()

    # --- Download ---
    data = cam.buffer_download(sequence=0)
    if data is not None:
        print(f"Downloaded: {data.shape}, {data.dtype}")
        np.save("buffer_data.npy", data)
        print("Saved to buffer_data.npy")

    # --- Clean up ---
    cam.buffer_clear()
