"""Test 5: Camera properties — read/write exposure, frame_rate,
calibration_mode, resolution. Test calibration mode lock issue.
"""
import sys, traceback
sys.path.insert(0, "C:/Users/jasas/Work/OpenSource/pyTelops")

from pyTelops import Camera
from pyTelops.registers import CalibrationMode

test_name = "Test 5: Camera properties"
cam = None
try:
    cam = Camera()
    cam.connect()
    print(f"Connected to {cam.camera_ip}")

    # --- Read exposure ---
    exp = cam.exposure
    print(f"Exposure: {exp} us")
    assert isinstance(exp, float), f"Expected float, got {type(exp)}"

    # --- Write exposure ---
    cam.exposure = 50.0
    exp2 = cam.exposure
    print(f"Exposure after set 50: {exp2} us")
    assert abs(exp2 - 50.0) < 1.0, f"Exposure not set: {exp2}"

    # Restore
    cam.exposure = exp
    print(f"Exposure restored: {cam.exposure} us")

    # --- Read frame rate ---
    fr = cam.frame_rate
    print(f"Frame rate: {fr} Hz")
    assert isinstance(fr, float), f"Expected float, got {type(fr)}"

    # --- Write frame rate ---
    cam.frame_rate = 100.0
    fr2 = cam.frame_rate
    print(f"Frame rate after set 100: {fr2} Hz")
    assert abs(fr2 - 100.0) < 1.0, f"Frame rate not set: {fr2}"

    # Restore
    cam.frame_rate = fr
    print(f"Frame rate restored: {cam.frame_rate} Hz")

    # --- Read calibration mode ---
    cal = cam.calibration_mode
    print(f"Calibration mode: {cal.name} ({cal.value})")
    assert isinstance(cal, CalibrationMode)

    # --- Write calibration mode ---
    # Switch to RAW then back
    target = CalibrationMode.RAW if cal != CalibrationMode.RAW else CalibrationMode.NUC
    print(f"Setting calibration mode to {target.name}...")
    cam.calibration_mode = target
    cal2 = cam.calibration_mode
    print(f"Calibration mode now: {cal2.name}")
    assert cal2 == target, f"CalibrationMode not set: {cal2} != {target}"

    # Restore
    cam.calibration_mode = cal
    cal3 = cam.calibration_mode
    print(f"Calibration mode restored: {cal3.name}")

    # --- Test calibration mode lock issue ---
    # Start stream (TLParamsLocked), then try to set calibration mode
    print("\n--- Calibration lock test ---")
    cam.start_stream()
    cam._gvcp.write_reg(0xD314, 1)  # ACQUISITION_START
    print("Stream active, trying to set calibration mode...")
    try:
        cam.calibration_mode = CalibrationMode.NUC
        cal_during_stream = cam.calibration_mode
        print(f"Calibration set during stream: {cal_during_stream.name}")
    except Exception as e:
        print(f"CalibrationMode locked during stream (expected): {e}")
        print("This is the known lock issue - setter should handle it")
    cam.stop_stream()
    # Restore calibration after stopping stream
    cam.calibration_mode = cal
    print(f"Calibration restored after stream stop: {cam.calibration_mode.name}")

    # --- Read resolution ---
    w, h = cam.resolution
    print(f"\nResolution: {w}x{h}")
    assert isinstance(w, int) and isinstance(h, int)
    assert w > 0 and h > 0

    # --- Read temperature ---
    temp = cam.temperature
    print(f"Temperature: {temp} C")

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
