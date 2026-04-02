"""Test 7: Buffer download speed — 500 frames.

Measure and report fps and MB/s.
"""
import sys, traceback, time
sys.path.insert(0, "C:/Users/jasas/Work/OpenSource/pyTelops")

from pyTelops import Camera
from pyTelops.registers import (
    MemoryBufferMOISource, MemoryBufferStatus
)

test_name = "Test 7: Buffer download speed (500 frames)"
cam = None
try:
    cam = Camera()
    cam.connect()
    print(f"Connected to {cam.camera_ip}")

    N_FRAMES = 500

    # --- Configure and record ---
    cam.buffer_configure(
        n_sequences=1,
        frames_per_seq=N_FRAMES,
        pre_moi=0,
        moi_source=MemoryBufferMOISource.SOFTWARE,
    )
    cam.buffer_arm()
    time.sleep(0.5)
    cam.buffer_fire_moi()

    # Wait for recording to finish
    deadline = time.monotonic() + 60.0
    while True:
        status = cam.buffer_status()
        if status in (MemoryBufferStatus.HOLDING, MemoryBufferStatus.IDLE):
            break
        if time.monotonic() > deadline:
            raise TimeoutError(f"Buffer stuck in {status.name}")
        time.sleep(0.5)

    n_recorded = cam.buffer_recorded_frames(sequence=0)
    print(f"Recorded {n_recorded} frames")

    # --- Download and measure speed ---
    t0 = time.monotonic()
    data = cam.buffer_download(sequence=0)
    t1 = time.monotonic()

    if data is None:
        raise RuntimeError("buffer_download() returned None")

    elapsed = t1 - t0
    fps = data.shape[0] / elapsed
    bytes_total = data.nbytes
    mbps = bytes_total / elapsed / 1e6

    print(f"\n=== Download Performance ===")
    print(f"Frames: {data.shape[0]}")
    print(f"Frame size: {data.shape[1]}x{data.shape[2]} x {data.dtype}")
    print(f"Total data: {bytes_total / 1e6:.1f} MB")
    print(f"Elapsed: {elapsed:.1f} s")
    print(f"Speed: {fps:.0f} fps, {mbps:.1f} MB/s")
    print(f"Data OK: min={data.min()}, max={data.max()}, mean={data.mean():.0f}")

    cam.buffer_clear()
    cam.disconnect()
    print(f"\n{test_name}: PASS")
except Exception:
    traceback.print_exc()
    print(f"\n{test_name}: FAIL")
    if cam:
        try:
            cam.disconnect()
        except Exception:
            pass
