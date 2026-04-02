"""
pyTelops — Pure-Python driver for Telops thermal cameras over GigE Vision.

Usage:
    from pyTelops import Camera, discover

    # Find cameras
    cameras = discover()

    # Connect and grab
    with Camera() as cam:
        frame = cam.grab()
"""

__version__ = "0.1.0"

from .camera import Camera, discover
from .gvcp import GVCPClient, GVCPError
from .registers import (
    CalibrationMode, ExposureAuto, TriggerSource, TriggerActivation,
    MemoryBufferMOISource, MemoryBufferStatus,
)

__all__ = [
    "Camera", "discover", "GVCPClient", "GVCPError",
    "CalibrationMode", "ExposureAuto", "TriggerSource",
    "TriggerActivation", "MemoryBufferMOISource", "MemoryBufferStatus",
    "__version__",
]
