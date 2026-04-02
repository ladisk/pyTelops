"""Run all hardware tests sequentially."""
import subprocess
import sys

PYTHON = "C:/Users/jasas/Work/OpenSource/OpenEOL/openEOL_venv/Scripts/python.exe"
TESTS = [
    "tests/hw_test_01_connect.py",
    "tests/hw_test_02_dirty_reconnect.py",
    "tests/hw_test_03_grab.py",
    "tests/hw_test_04_grab_timeout.py",
    "tests/hw_test_05_properties.py",
    "tests/hw_test_06_buffer.py",
    "tests/hw_test_07_download_speed.py",
    "tests/hw_test_08_stream.py",
    "tests/hw_test_09_state.py",
    "tests/hw_test_10_error_recovery.py",
]

results = []
for test in TESTS:
    print(f"\n{'='*60}")
    print(f"Running {test}")
    print(f"{'='*60}")
    result = subprocess.run(
        [PYTHON, test],
        capture_output=False,
        timeout=120,
    )
    results.append((test, result.returncode))

print(f"\n{'='*60}")
print("SUMMARY")
print(f"{'='*60}")
for test, rc in results:
    status = "OK" if rc == 0 else f"EXIT CODE {rc}"
    print(f"  {test}: {status}")
