"""Check camera state registers."""
import sys
sys.path.insert(0, "C:/Users/jasas/Work/OpenSource/pyTelops")
from pyTelops import Camera
from pyTelops import registers as reg

cam = Camera()
cam.connect()
print(f"Info: {cam.info}")
print(f"State: {cam.state}")
print(f"Buffer status: {cam.buffer_status().name}")

# Try turning buffer off
try:
    cam.write_register(reg.REG_MEMORY_BUFFER_MODE, reg.MemoryBufferMode.OFF)
    print("Buffer mode set to OFF")
except Exception as e:
    print(f"Failed to set buffer OFF: {e}")

# Now try ACQUISITION_START
try:
    cam.start_stream()
    cam.write_register(reg.REG_ACQUISITION_START, 1)
    print("ACQUISITION_START succeeded")
    import time
    time.sleep(0.5)
    frame = cam._gvsp.get_frame(timeout=5.0)
    if frame is not None:
        print(f"Got frame: {frame.shape}")
    else:
        print("No frame received")
    cam.stop_stream()
except Exception as e:
    print(f"Failed: {e}")

cam.disconnect()
