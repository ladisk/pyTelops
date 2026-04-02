"""Test 3: Grab a single frame — verify shape/dtype."""
import sys, traceback
sys.path.insert(0, "C:/Users/jasas/Work/OpenSource/pyTelops")

from pyTelops import Camera

test_name = "Test 3: Grab single frame"
try:
    cam = Camera()
    cam.connect()
    print(f"Connected to {cam.camera_ip}")
    w, h = cam.resolution
    print(f"Resolution: {w}x{h}")

    frame = cam.grab(timeout=10.0)
    if frame is None:
        raise RuntimeError("grab() returned None")

    print(f"Frame shape: {frame.shape}, dtype: {frame.dtype}")
    print(f"Min/Max: {frame.min()} / {frame.max()}")

    # Verify shape matches reported resolution
    assert frame.ndim == 2, f"Expected 2D, got {frame.ndim}D"
    assert frame.shape == (h, w), f"Shape {frame.shape} != expected ({h}, {w})"
    assert frame.dtype in ("uint16", "uint8"), f"Unexpected dtype {frame.dtype}"

    cam.disconnect()
    print(f"\n{test_name}: PASS")
except Exception:
    traceback.print_exc()
    print(f"\n{test_name}: FAIL")
    try:
        cam.disconnect()
    except Exception:
        pass
