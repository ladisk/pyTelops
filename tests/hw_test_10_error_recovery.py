"""Test 10: Error recovery.

Connect, start stream, force-disconnect without stopping stream
(simulate crash), then create new Camera and connect. Should work.
"""
import sys, traceback, time
sys.path.insert(0, "C:/Users/jasas/Work/OpenSource/pyTelops")

from pyTelops import Camera
from pyTelops import registers as reg

test_name = "Test 10: Error recovery after forced disconnect"
cam = None
cam2 = None
try:
    cam = Camera()
    cam.connect()
    ip = cam.camera_ip
    print(f"cam1 connected to {ip}")

    # Start stream
    cam.start_stream()
    cam._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)
    print("Stream started")

    # Grab a frame to confirm stream is working
    frame = cam._gvsp.get_frame(timeout=5.0)
    print(f"Got frame: {frame.shape if frame is not None else 'None'}")

    # Force-disconnect: close socket without proper cleanup
    # This simulates a crash / kernel restart
    print("\nForce-disconnecting (simulating crash)...")
    cam._gvcp._heartbeat_stop.set()  # stop heartbeat
    cam._gvcp._connected = False
    cam._gvcp._sock.close()
    cam._gvcp._sock = None
    cam._connected = False
    cam._streaming = False
    # Now cam is in a broken state; CCP will timeout after heartbeat period

    # Create new Camera and connect
    print("Creating new Camera and connecting...")
    cam2 = Camera(ip=ip)
    t0 = time.monotonic()
    cam2.connect()
    elapsed = time.monotonic() - t0
    print(f"cam2 connected in {elapsed:.1f}s")

    # Wait briefly for camera to become ready after recovery
    time.sleep(0.5)

    # Verify it works
    s = cam2.state
    print(f"cam2 state: {s}")
    assert s == "connected", f"Expected 'connected', got '{s}'"

    # Grab a frame to confirm camera is functional
    frame2 = cam2.grab(timeout=10.0)
    print(f"cam2 grab: {frame2.shape if frame2 is not None else 'None'}")
    assert frame2 is not None, "Failed to grab frame after recovery"

    cam2.disconnect()
    print(f"\n{test_name}: PASS")
except Exception:
    traceback.print_exc()
    print(f"\n{test_name}: FAIL")
    for c in (cam, cam2):
        if c:
            try:
                c.disconnect()
            except Exception:
                pass
