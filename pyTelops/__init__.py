"""
pyTelops -- Pure-Python driver for Telops thermal cameras over GigE Vision.

Usage::

    from pyTelops import Camera, discover

    # Find cameras on the local network
    cameras = discover()

    # Connect and grab a single frame
    with Camera() as cam:
        frame = cam.grab()
"""

from __future__ import annotations

__version__ = "0.2.1"
from pyGigEVision import GVCPClient, GVCPError

from .camera import Camera, discover
from .provisioning import force_ip
from .registers import (
    CalibrationCollectionType,
    CalibrationMode,
    CurrentLocation,
    ExposureAuto,
    FrameRateMode,
    ImageCorrectionMode,
    MemoryBufferMOISource,
    MemoryBufferStatus,
    TemperatureLocation,
    TestImageSelector,
    TriggerActivation,
    TriggerSource,
    VoltageLocation,
)

# Thermal camera convention alias
IntegrationTimeAuto = ExposureAuto

__all__ = [
    "Camera",
    "discover",
    "force_ip",
    "GVCPClient",
    "GVCPError",
    "CalibrationMode",
    "ExposureAuto",
    "IntegrationTimeAuto",
    "TriggerSource",
    "TriggerActivation",
    "MemoryBufferMOISource",
    "MemoryBufferStatus",
    "FrameRateMode",
    "TestImageSelector",
    "ImageCorrectionMode",
    "TemperatureLocation",
    "VoltageLocation",
    "CurrentLocation",
    "CalibrationCollectionType",
    "__version__",
]
