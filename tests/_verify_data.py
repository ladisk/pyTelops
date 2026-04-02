"""Quick check that downloaded buffer data is not all zeros."""
import sys, time
sys.path.insert(0, "C:/Users/jasas/Work/OpenSource/pyTelops")
from pyTelops import Camera
from pyTelops.registers import MemoryBufferMOISource

cam = Camera()
cam.connect()
cam.buffer_configure(n_sequences=1, frames_per_seq=20, pre_moi=5,
                     moi_source=MemoryBufferMOISource.SOFTWARE)
cam.buffer_arm()
time.sleep(0.5)
cam.buffer_fire_moi()

# Wait for recording to finish
from pyTelops.registers import MemoryBufferStatus
deadline = time.monotonic() + 15
while True:
    status = cam.buffer_status()
    print(f"  buffer status: {status.name}")
    if status in (MemoryBufferStatus.HOLDING, MemoryBufferStatus.IDLE):
        break
    if time.monotonic() > deadline:
        print("Timeout waiting for buffer!")
        break
    time.sleep(0.5)

n = cam.buffer_recorded_frames()
print(f"Recorded: {n}")
data = cam.buffer_download()
if data is not None:
    print(f"Shape: {data.shape}, dtype: {data.dtype}")
    print(f"Min: {data.min()}, Max: {data.max()}, Mean: {data.mean():.1f}")
    print(f"Frame 0 center pixel: {data[0, 129, 160]}")
    print(f"All zeros? {data.max() == 0}")
else:
    print("No data!")
cam.buffer_clear()
cam.disconnect()
