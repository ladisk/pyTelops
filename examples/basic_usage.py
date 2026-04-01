"""Basic pyTelops usage examples."""

from pyTelops import Camera, discover

# --- 1. Discover cameras ---
cameras = discover()
for cam in cameras:
    print(f"{cam['manufacturer']} {cam['model']} at {cam['ip']}")

# --- 2. Connect and grab a frame ---
with Camera() as cam:
    print(cam.info)

    # Set exposure and frame rate
    cam.exposure = 50.0        # microseconds
    cam.frame_rate = 100.0     # Hz

    # Single frame
    frame = cam.grab()
    print(f"Frame: {frame.shape}, {frame.dtype}")

    # Multiple frames
    frames = cam.acquire(10)
    print(f"Batch: {frames.shape}")
