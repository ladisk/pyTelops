"""Test 4: Grab when frame times out.

Connect but DON'T start acquisition manually, call grab with timeout=2.
Should return None, not crash.
"""
import sys, traceback, time
sys.path.insert(0, "C:/Users/jasas/Work/OpenSource/pyTelops")

from pyTelops import Camera

test_name = "Test 4: Grab with timeout (no acquisition)"
try:
    cam = Camera()
    cam.connect()
    print(f"Connected to {cam.camera_ip}")

    # Start the stream but don't start acquisition — so no frames will come
    cam.start_stream()
    print("Stream started (no ACQUISITION_START)")

    # Try to get a frame — should timeout and return None
    t0 = time.monotonic()
    result = cam._gvsp.get_frame(timeout=2.0)
    elapsed = time.monotonic() - t0
    print(f"get_frame returned after {elapsed:.1f}s: {result}")

    assert result is None, f"Expected None, got {type(result)}"
    assert elapsed >= 1.5, f"Returned too fast ({elapsed:.1f}s < 1.5s)"

    cam.stop_stream()
    cam.disconnect()
    print(f"\n{test_name}: PASS")
except Exception:
    traceback.print_exc()
    print(f"\n{test_name}: FAIL")
    try:
        cam.disconnect()
    except Exception:
        pass
