"""Test 6: Buffer full workflow.

Connect, configure buffer (100 frames, SOFTWARE MOI), arm, start,
fire MOI, wait for HOLDING/IDLE (with timeout!), read recorded count,
download, verify array shape, clear buffer, disconnect.
"""
import sys, traceback, time
sys.path.insert(0, "C:/Users/jasas/Work/OpenSource/pyTelops")

from pyTelops import Camera
from pyTelops.registers import (
    MemoryBufferMOISource, MemoryBufferStatus, MemoryBufferMode
)

test_name = "Test 6: Buffer full workflow"
cam = None
try:
    cam = Camera()
    cam.connect()
    print(f"Connected to {cam.camera_ip}")

    N_FRAMES = 100

    # --- Configure buffer ---
    cam.buffer_configure(
        n_sequences=1,
        frames_per_seq=N_FRAMES,
        pre_moi=10,
        moi_source=MemoryBufferMOISource.SOFTWARE,
    )
    status = cam.buffer_status()
    print(f"After configure: buffer status = {status.name}")

    # --- Arm and start ---
    cam.buffer_arm()
    print("Buffer armed, acquisition started")
    time.sleep(0.5)

    # --- Fire MOI ---
    cam.buffer_fire_moi()
    print("MOI fired")

    # --- Wait for HOLDING or IDLE (with timeout) ---
    deadline = time.monotonic() + 30.0
    while True:
        status = cam.buffer_status()
        if status in (MemoryBufferStatus.HOLDING, MemoryBufferStatus.IDLE):
            break
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Buffer stuck in {status.name} after 30s")
        time.sleep(0.5)
    print(f"Buffer finished: status = {status.name}")

    # --- Read recorded count ---
    n_recorded = cam.buffer_recorded_frames(sequence=0)
    print(f"Recorded frames: {n_recorded}")
    assert n_recorded > 0, "No frames recorded!"
    assert n_recorded <= N_FRAMES, f"More frames than expected: {n_recorded}"

    # --- Download ---
    data = cam.buffer_download(sequence=0)
    if data is None:
        raise RuntimeError("buffer_download() returned None")
    print(f"Downloaded: shape={data.shape}, dtype={data.dtype}")
    assert data.ndim == 3, f"Expected 3D array, got {data.ndim}D"
    assert data.shape[0] == n_recorded, (
        f"Frame count mismatch: {data.shape[0]} != {n_recorded}")

    # --- Clear buffer ---
    cam.buffer_clear()
    time.sleep(0.5)
    status = cam.buffer_status()
    print(f"After clear: buffer status = {status.name}")

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
