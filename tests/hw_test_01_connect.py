"""Test 1: Basic connect/disconnect and reconnection."""
import sys, traceback
sys.path.insert(0, "C:/Users/jasas/Work/OpenSource/pyTelops")

from pyTelops import Camera

test_name = "Test 1: Basic connect/disconnect"
try:
    # First connection
    cam = Camera()
    cam.connect()
    print(f"Connected to {cam.camera_ip}")
    print(f"Info: {cam.info}")
    cam.disconnect()
    print("Disconnected OK")

    # Second connection (re-connection to same camera)
    cam2 = Camera()
    cam2.connect()
    print(f"Re-connected to {cam2.camera_ip}")
    cam2.disconnect()
    print("Second disconnect OK")

    print(f"\n{test_name}: PASS")
except Exception:
    traceback.print_exc()
    print(f"\n{test_name}: FAIL")
