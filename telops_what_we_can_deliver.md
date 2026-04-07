# LADISK — Telops: Technical Capability Overview

---

## Already Delivered: fasthcc

Open-source Python package for reading and writing Telops HCC thermal image files. Published on PyPI (`pip install fasthcc`), available on GitHub.

- up to **100× faster** than TelopsToolbox
- Full read and write support for all HCC file versions (V5 through V12)
- Calibrated (radiometric temperature) and raw readout modes
- Streaming writer for real-time frame-by-frame file creation
- Per-frame metadata access (timestamps, calibration parameters, camera settings)
- Written files verified compatible with Telops Reveal software
- Pure Python with numpy — no compiled extensions, no SDK required
- Tested on Python 3.9–3.13, MIT licensed

---

## What We Can Deliver: pyTelops

A modern Python SDK for **full programmatic control** of Telops cameras. Simple `pip install`, no compiled code, no additional dependencies — enabling automated measurements, custom data pipelines, and integration into scientific and industrial workflows.

**Complete Programmatic Camera Control**
- Connect, configure, and stream from Telops GigE Vision cameras directly from Python
- All camera settings as simple properties: integration time, frame rate, calibration, resolution, trigger, ROI
- Live streaming over GigE at full bandwidth
- Buffer download optimized to saturate GigE bandwidth — up to 45× faster than default camera transfer settings
- Cross-platform: Windows, Linux, and macOS

**Lightweight Web-Based Interface**
- Modern browser-based camera operation — live view, recording, calibration
- Accessible from devices on the local network — no camera software needed on the viewing device
- Remote lab access from anywhere

**Multi-Camera Synchronization**
- Control and stream from multiple Telops cameras simultaneously
- Synchronized via external trigger or GEV timestamps
- Multi-angle thermal measurements, stereo thermography, and large-area coverage

**Multi-Sensor Fusion**
- Synchronize Telops thermal cameras with accelerometers, strain gauges, force sensors, and high-speed visible cameras
- One unified timeline, one API — combining thermal, mechanical, and optical data
- Coupled thermo-mechanical measurements that currently require significant custom integration

**Real-Time Processing**
- Live frame processing at camera frame rate — analysis happens during acquisition, not after
- Custom processing pipelines: filtering, averaging, anomaly detection, thermal mapping
- Immediate visual feedback — see results as the measurement runs
- Enables use cases that require instant decisions: production-line reject/pass, safety monitoring, process control

**Plugin Ecosystem**
- Extensible plugin architecture for domain-specific modules
- Non-destructive testing, process monitoring, environmental testing, R&D analysis, and more
- Researchers and engineers develop and share analysis tools
- Foundation for a growing open-source ecosystem around Telops cameras

**Automated Measurement & Hardware-in-the-Loop Testing**
- Scriptable measurement campaigns: configure, calibrate, record, download, process, export
- Fully unattended operation from a single Python script
- Real-time temperature feedback to control loops — closed-loop thermal testing
- Programmable trigger logic based on temperature thresholds, external events, or test sequences
- Production-line quality control, long-duration monitoring, accelerated life testing

**Documentation & Training**
- Comprehensive API reference and user guides
- Jupyter notebook tutorials for common use cases
- Example workflows for common applications: non-destructive testing, process monitoring, heat transfer analysis, stress analysis
- Training materials suitable for university teaching and industrial onboarding

---

### Vision

A complete, modern, open-source Python ecosystem for Telops thermal cameras — from fast file I/O through full camera control to real-time processing, web-based operation, and domain-specific analysis tools. Built by researchers who use these cameras daily, designed to make Telops cameras the most accessible thermal imaging platform for researchers and engineers.
