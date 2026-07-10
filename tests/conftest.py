"""Test configuration and fixtures."""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--hardware",
        action="store_true",
        default=False,
        help="Run tests that require a connected camera",
    )
    parser.addoption(
        "--power-cycle",
        action="store_true",
        default=False,
        help="Also run tests that cycle the camera power state or reboot it "
        "(slow, minutes to re-cool; wears the Stirling cooler)",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "hardware: test requires a connected Telops camera")
    config.addinivalue_line("markers", "slow: test takes >60s (multi-sequence buffer operations)")
    config.addinivalue_line(
        "markers",
        "power_cycle: test cycles the camera cooler or reboots it (needs --power-cycle)",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--hardware"):
        skip = pytest.mark.skip(reason="Need --hardware flag to run")
        for item in items:
            if "hardware" in item.keywords:
                item.add_marker(skip)
    if not config.getoption("--power-cycle"):
        skip_pc = pytest.mark.skip(
            reason="Need --power-cycle flag (cycles cooler / reboots camera)"
        )
        for item in items:
            if "power_cycle" in item.keywords:
                item.add_marker(skip_pc)
