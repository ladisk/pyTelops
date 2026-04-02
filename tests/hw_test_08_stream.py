"""Test 8: Live stream start/stop.

Connect, start_stream, grab a few frames via _gvsp.get_frame_with_info(),
stop_stream. Then start again and stop again (test restart works).
"""
import sys, traceback
sys.path.insert(0, "C:/Users/jasas/Work/OpenSource/pyTelops")

from pyTelops import Camera
from pyTelops import registers as reg

test_name = "Test 8: Live stream start/stop"
cam = None
try:
    cam = Camera()
    cam.connect()
    print(f"Connected to {cam.camera_ip}")

    # --- First stream cycle ---
    cam.start_stream()
    cam._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)
    print("Stream 1 started")

    frames_1 = []
    for i in range(5):
        result = cam._gvsp.get_frame_with_info(timeout=5.0)
        if result is not None:
            frame, info = result
            frames_1.append(frame)
            print(f"  Frame {i}: shape={frame.shape}, "
                  f"block_id={info['block_id']}, "
                  f"missing_packets={info['missing_packets']}")
        else:
            print(f"  Frame {i}: None (timeout)")

    cam.stop_stream()
    print(f"Stream 1 stopped, got {len(frames_1)} frames")
    assert len(frames_1) >= 3, f"Too few frames: {len(frames_1)}"

    # --- Second stream cycle (test restart) ---
    cam.start_stream()
    cam._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)
    print("\nStream 2 started")

    frames_2 = []
    for i in range(3):
        result = cam._gvsp.get_frame_with_info(timeout=5.0)
        if result is not None:
            frame, info = result
            frames_2.append(frame)
            print(f"  Frame {i}: shape={frame.shape}, "
                  f"block_id={info['block_id']}")
        else:
            print(f"  Frame {i}: None (timeout)")

    cam.stop_stream()
    print(f"Stream 2 stopped, got {len(frames_2)} frames")
    assert len(frames_2) >= 2, f"Too few frames in restart: {len(frames_2)}"

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
