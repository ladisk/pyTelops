"""External trigger example.

Configure the camera to start acquisition on a BNC trigger signal.
"""

import numpy as np
from pyTelops import Camera
from pyTelops.registers import (
    TriggerSource, TriggerActivation, MemoryBufferMOISource,
)

with Camera() as cam:
    # --- Configure external trigger ---
    cam.configure_trigger(
        source=TriggerSource.EXTERNAL_SIGNAL,
        activation=TriggerActivation.RISING_EDGE,
        enabled=True,
    )
    print("External trigger armed (waiting for BNC rising edge)")

    # --- Option A: Live capture on trigger ---
    frame = cam.grab(timeout=30.0)  # waits up to 30s for trigger
    if frame is not None:
        print(f"Captured: {frame.shape}")

    # --- Option B: Buffer recording with external MOI ---
    cam.buffer_configure(
        n_sequences=1,
        frames_per_seq=500,
        pre_moi=100,  # 100 frames before trigger
        moi_source=MemoryBufferMOISource.EXTERNAL_SIGNAL,
    )
    cam.buffer_arm()
    print("Buffer armed, waiting for external MOI signal...")

    # The camera will record automatically when BNC trigger fires.
    # Poll buffer status to know when it's done:
    import time
    from pyTelops.registers import MemoryBufferStatus

    while True:
        status = cam.buffer_status()
        if status == MemoryBufferStatus.HOLDING:
            break
        time.sleep(0.5)

    data = cam.buffer_download()
    np.save("triggered_data.npy", data)
    print(f"Saved {data.shape} to triggered_data.npy")
