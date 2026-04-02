"""Test 9: State transitions.

Verify cam.state returns correct values:
- "disconnected" before connect
- "connected" after connect
- "streaming" during stream
- "disconnected" after disconnect
"""
import sys, traceback
sys.path.insert(0, "C:/Users/jasas/Work/OpenSource/pyTelops")

from pyTelops import Camera
from pyTelops import registers as reg

test_name = "Test 9: State transitions"
cam = None
try:
    cam = Camera()

    # Before connect
    s = cam.state
    print(f"Before connect: state = '{s}'")
    assert s == "disconnected", f"Expected 'disconnected', got '{s}'"

    # After connect
    cam.connect()
    s = cam.state
    print(f"After connect: state = '{s}'")
    assert s == "connected", f"Expected 'connected', got '{s}'"

    # During stream
    cam.start_stream()
    cam._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)
    s = cam.state
    print(f"During stream: state = '{s}'")
    assert s == "streaming", f"Expected 'streaming', got '{s}'"

    # After stop stream
    cam.stop_stream()
    s = cam.state
    print(f"After stop_stream: state = '{s}'")
    assert s == "connected", f"Expected 'connected', got '{s}'"

    # After disconnect
    cam.disconnect()
    s = cam.state
    print(f"After disconnect: state = '{s}'")
    assert s == "disconnected", f"Expected 'disconnected', got '{s}'"

    print(f"\n{test_name}: PASS")
except Exception:
    traceback.print_exc()
    print(f"\n{test_name}: FAIL")
    if cam:
        try:
            cam.disconnect()
        except Exception:
            pass
