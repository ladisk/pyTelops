"""Basic pyTelops usage examples."""

from pyTelops import Camera, discover

# --- 1. Discover cameras ---
cameras = discover()
for cam in cameras:
    print(f"{cam['manufacturer']} {cam['model']} at {cam['ip']}")

# --- 2. Connect and grab a frame ---
with Camera() as cam:
    print(cam.info)

    # Configure
    cam.calibration_mode = "RT"
    cam.exposure_auto = "continuous"

    # Single frame (headers stripped automatically)
    frame = cam.grab()
    print(f"Frame: {frame.shape}, {frame.dtype}")

    # Multiple frames via live streaming
    frames = cam.acquire(10)
    print(f"Batch: {frames.shape}")
