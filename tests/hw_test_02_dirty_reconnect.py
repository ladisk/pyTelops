"""Test 2: Forced reconnect after dirty exit.

Connect, do NOT disconnect, create a NEW Camera and connect.
This simulates kernel restart / ACCESS_DENIED scenario.
The package should handle this gracefully (poll until heartbeat expires).
"""
import sys, traceback, time
sys.path.insert(0, "C:/Users/jasas/Work/OpenSource/pyTelops")

from pyTelops import Camera

test_name = "Test 2: Forced reconnect after dirty exit"
try:
    # First connection — connect normally
    cam1 = Camera()
    cam1.connect()
    print(f"cam1 connected to {cam1.camera_ip}")
    ip = cam1.camera_ip  # save IP for cam2

    # Intentionally do NOT disconnect cam1 (simulate crash)
    # cam1's heartbeat thread is still running, holding CCP

    # Create a NEW Camera and try to connect
    # The package should handle ACCESS_DENIED by polling
    print("Creating cam2 without disconnecting cam1...")
    t0 = time.monotonic()
    cam2 = Camera(ip=ip)
    cam2.connect()
    elapsed = time.monotonic() - t0
    print(f"cam2 connected after {elapsed:.1f}s")

    # Clean up
    cam2.disconnect()
    print("cam2 disconnected OK")

    # cam1 is now stale (cam2 took control and released it)
    # Try to clean up cam1's heartbeat thread
    try:
        cam1.disconnect()
    except Exception:
        pass

    print(f"\n{test_name}: PASS")
except Exception:
    traceback.print_exc()
    print(f"\n{test_name}: FAIL")
    # Emergency cleanup
    try:
        cam1.disconnect()
    except Exception:
        pass
