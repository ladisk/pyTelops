# pyTelops — Development Report

## Overview

**pyTelops** is a pure-Python driver for Telops FAST-series MWIR thermal cameras over GigE Vision. It communicates directly via GVCP/GVSP protocols over UDP — no vendor SDK, no compiled extensions.

- **Camera**: Telops FAST M3k — InSb MWIR detector, 320×256, 25 mK sensitivity, 16 GB internal buffer
- **Connection**: GigE Vision 1.2 over Ethernet (link-local 169.254.x.x)
- **Repository**: https://github.com/jasasonc/pyTelops
- **Author**: Jaša Šonc, University of Ljubljana

---

## Motivation

The Telops FAST M3k ships with RevealIR (GUI only) and TelopsToolbox (reads .HCC files only). The Pleora eBUS SDK installed on the system was too old for Python bindings (v5.1.5, need 6.3+). There was no way to programmatically control the camera for automated measurements, integrate with data acquisition frameworks (openEOL, LDAQ), or build custom measurement pipelines.

The goal was a standalone Python package that could:
1. Discover and connect to the camera automatically
2. Control all camera settings (integration time, frame rate, calibration, resolution)
3. Stream live frames and record high-speed sequences to the internal buffer
4. Download recorded data efficiently
5. Provide a live viewer for framing shots
6. Plug into openEOL and LDAQ as a backend

---

## Architecture

The driver has four layers:

```
┌──────────────────────────────────────┐
│  Camera class (camera.py)            │  User-facing API: properties,
│  - Properties, context manager       │  context manager, auto-discovery
│  - Buffer recording, calibration     │
├──────────────┬───────────────────────┤
│  GVCPClient  │  GVSPReceiver         │
│  (gvcp.py)   │  (gvsp.py)           │
│  Control     │  Streaming            │
│  UDP :3956   │  UDP dynamic port     │
├──────────────┴───────────────────────┤
│  registers.py                        │  65+ register addresses,
│  16 IntEnum classes                  │  parsed from GenICam XML
└──────────────────────────────────────┘
```

### GVCP (Control Protocol)

Request-response over UDP port 3956. Every command is an 8-byte header + payload:
```
[0x42] [flags] [command: 2B] [payload_len: 2B] [req_id: 2B]
```

Commands: DISCOVERY (broadcast), READREG, WRITEREG, READMEM, PACKETRESEND.

A heartbeat thread reads the CCP register every 2 seconds to maintain the session.

### GVSP (Streaming Protocol)

Camera pushes UDP packets to a host-specified IP:port. Each frame arrives as:
```
[Leader]  →  metadata: width, height, pixel format, timestamp
[Data 1]  →  1464 bytes of pixel data
[Data 2]  →  1464 bytes
  ...
[Data N]  →  last chunk
[Trailer] →  frame complete signal
```

A background thread receives packets, assembles frames into pre-allocated numpy buffers, and pushes completed frames onto a queue.

### Register Map

65+ register addresses extracted from the camera's GenICam XML file (579 KB, 387 registers, 525 features). The XML was downloaded directly from the camera via GVCP READMEM.

---

## Development Journey

### Phase 1: Protocol Implementation

Built the GVCP client from the GigE Vision specification — discovery broadcast, register read/write, memory read, heartbeat keepalive. Then built the GVSP receiver — UDP frame reassembly from Leader/Data/Trailer packets.

**8 bugs found and fixed during initial development:**

1. **GVCP req_id wrong size** — packed as 4 bytes instead of 2, causing all reads to return PACKET_REMOVED
2. **ExposureTime locked by AEC** — Auto Exposure Control locks the register; driver auto-disables AEC before writing
3. **Discovery string offsets wrong** — Telops uses extended discovery format with +24 byte offset vs standard spec
4. **GVSP packet type in wrong bits** — type is in lower 3 bits of format byte, not upper nibble
5. **Packet size exceeds MTU** — camera default 8228 bytes, network MTU 1500; auto-clamped on stream start
6. **Buffer download register order** — DownloadMode must be set BEFORE FrameID/FrameCount (GenICam pIsLocked dependency)
7. **IOBase compatibility** — logger_name required, state is read-only
8. **GVSP byteswap wrong** — Telops sends little-endian (unusual for GigE Vision); removed incorrect byteswap

### Phase 2: Standalone Package

Extracted the driver from the openEOL workspace into a standalone package following ladisk group conventions (flat layout, hatchling build, MIT license, RST README). Key design decisions:

- **No openeol/LDAQ dependency** — standalone, frameworks wrap it externally
- **Context manager** — `with Camera() as cam:` for reliable connect/disconnect
- **Auto-discovery** — finds cameras on any link-local interface
- **Properties** for all camera settings instead of getter/setter methods

### Phase 3: Buffer Download Speed Optimization

Initial buffer download: **15 fps / 2.5 MB/s** (10 frames in 1.7s).

**Root cause discovered**: The `DownloadBitrateMax` register (0xEAD4) defaults to 20 Mbps. Maximum allowed: 1000 Mbps. But the register is **locked when DownloadMode = OFF** — must set mode to SEQUENCE first.

Also tested packet sizes: 1500 → 3000 → 6000 → 9000 bytes.

**Results after optimization**:

| Config | FPS | MB/s | Speedup |
|---|---|---|---|
| Before (20 Mbps, 1500B) | 15 | 2.5 | 1× |
| 1000 Mbps, 1500B | 278 | 45.9 | **18×** |
| 1000 Mbps, 3000B | 533 | 88.1 | **36×** |
| 1000 Mbps, 9000B | 673 | 111.2 | **45×** |

Default is 1500B packet size (safe on all networks). Large packets use IP fragmentation which can cause data loss on some networks.

### Phase 4: Robustness & Connection Handling

**Problems encountered:**
- Kernel restart leaves camera locked (ACCESS_DENIED on reconnect)
- Buffer recording with stale data locks register writes
- Calibration mode locked when buffer has data
- Camera cooling down causes cryptic GENERIC_ERROR

**Solutions implemented:**
- `_active_cameras` registry auto-disconnects stale instances in same process
- GVCP connect polls on ACCESS_DENIED until heartbeat expires (up to 15s)
- `buffer_configure()` auto-clears stale buffer data on failure
- `wait_until_ready()` polls TDC Status register (0xEAAC) with human-readable messages
- Auto-wait on connect and before acquisition

### Phase 5: GVCP Protocol Hardening (Aravis-Inspired)

Studied the aravis open-source GigE Vision library to improve protocol robustness:

- **ACK ID validation** — discards stale packets from previous commands, loops until correct response
- **3 retries with command resend** on timeout (was single receive retry)
- **PENDING_ACK handling** — camera sends 0x0089 when it needs more time; extends deadline
- **Control loss detection** — heartbeat checks if CCP bits cleared, sets `_control_lost` flag

### Phase 6: GVSP Rewrite (Aravis-Inspired)

Complete rewrite of the streaming receiver:

- **Pre-allocated frame buffers** — `write_packet()` copies data directly to the correct byte offset in a pre-allocated bytearray, instead of storing in a dict and sorting+joining at assembly time
- **Payload size auto-detection** — the camera's actual GVSP data payload (1464 bytes) differs from the assumed `packet_size - 8` (1492 bytes) due to extended headers. Auto-detected from the first received packet.
- **Direct packet resend** — sends PACKETRESEND directly from the GVSP stream socket to the camera's GVCP port, avoiding the GVCP client lock
- **Real-time gap detection** — checks for missing packets on every received packet, not just at trailer time
- **Three-tier timeouts** — 5ms initial grace, 20ms resend interval, 200ms frame retention (was single 5s timeout)

**Critical bug found during rewrite**: The pre-allocated buffer used `packet_size - 8 = 1492` as the offset stride, but actual payloads were 1464 bytes (36-byte GVSP header, not 8). This 28-byte accumulated offset per packet caused a **diagonal pixel shift pattern** visible in the image. Fixed by auto-detecting payload size from the first data packet.

### Phase 7: Camera Features

**GenICam XML deep dive** — analyzed all 387 registers to discover undocumented features:

| Feature | What it does |
|---|---|
| NUC trigger (0xE888) | Programmatic non-uniformity correction |
| Bad pixel replacement (0xEB60) | Auto-replace dead pixels (ON by default) |
| TDC Status (0xEAAC) | 16-bit bitmask: why camera is not ready |
| 13 temperature sensors | Sensor, compressor, FPGA, cold finger, etc. |
| Voltage/current monitoring | Cooler, supply rails |
| ROI offset (0xEB44/0xEB48) | Subwindow positioning |
| Frame rate mode (0xE818) | Fixed, FixedLocked, Maximum, Burst |
| Reverse X/Y (0xE8D4/0xE8D8) | Camera-side image flip |
| Test image generator (0xEACC) | Synthetic images for testing |
| GEV timestamps (0x093C-0x094C) | Precise frame timing |
| POSIX time (0xE980) | Camera clock sync |
| Save configuration (0xEC34) | Persist settings to camera memory |

### Phase 8: Multi-Sequence Buffer Recording

**Problem**: With `n_sequences > 1`, the camera stays in RECORDING state until ALL sequences are filled. `buffer_status()` never transitions to HOLDING/IDLE until the last MOI fires. Per-sequence frame counts are locked during recording.

**Solution discovered from GenICam XML**: `MemoryBufferSequenceCount` register (0xE914) increments as each sequence completes. Poll this instead of `buffer_status()` for per-sequence completion detection.

`buffer_record()` now handles multi-sequence automatically — arms once, fires MOI for each sequence, polls SEQ_COUNT between sequences, stops acquisition after the last.

### Phase 9: Resolution Constraints

**Empirically discovered** (not documented by Telops):
- **Width**: must be multiple of 64. Valid: 64, 128, 192, 256, 320
- **Height**: must be multiple of 4. Valid: 4, 8, 12, ..., 252, 256 (usable pixels)
- Camera registers accept any value, but only these produce GVSP frames
- Resolution is presented to the user in **usable pixels** — the 2 header rows are added internally

RevealIR enforces the same constraints in its UI (step=64 for width, step=4 for height).

**FPS at different resolutions (measured)**:

| Resolution | Int. time | Max FPS |
|---|---|---|
| 320×256 (full) | 10 µs | 3,115 |
| 320×128 | 10 µs | 5,973 |
| 320×64 | 10 µs | 11,034 |
| 128×64 | 10 µs | 17,836 |
| 64×32 | 10 µs | 36,676 |
| 64×4 | 5 µs | 95,184 |

### Phase 10: Calibration System

**Problem**: The camera stores calibration collections (one per lens + filter wheel position), but they're identified only by POSIX timestamps. Lens names are nowhere in the camera registers.

**Solution**: Lens names and temperature ranges are in calibration files on the USB drive shipped with the camera. The driver parses `.tsco` filenames and `estimated_ExposureTimes/*.txt` files to map timestamps to lens names.

```python
cam.load_calibration_info("path/to/TEL-8050 Calibration Data/")
cam.calibration_load(lens="50mm", temp=25)   # auto-selects right collection
```

**Gotchas discovered**:
- `.tsco` files use 0-indexed FW positions (FW0-FW3), exposure files use 1-indexed (FW1-FW4)
- `.tsco` uses `EL08887`, exposure files use `ELSN08887`
- Loading the same collection twice fails with GENERIC_ERROR — driver skips if already active

### Phase 11: RT Mode Temperature Conversion

**Problem**: `cam.grab()` in RT mode returned raw uint16 values (~7000 for a hand), not temperatures.

**Investigation**: Brute-forced every power of 2 as a scaling factor. Found `pixel * 2^(-8) + 273.15 = Kelvin`. Confirmed by pointing camera at a hand (28.9°C).

**Key discovery**: The conversion parameters `DataExp` and `DataOffset` are stored in each frame's **2-row Telops header** — the same header rows we strip. The driver now reads these values from the header before stripping it.

```
T_kelvin = pixel × 2^DataExp + DataOffset    (DataExp=-8, DataOffset=273.15)
T_celsius = T_kelvin - 273.15
```

The conversion is confirmed across the TelopsToolbox source code, HCC Header Reference PDF, and fasthcc package. The driver outputs **Celsius** by default (matches RevealIR and calibration data).

### Phase 12: Live Viewer

Tkinter-based real-time thermal display with:
- Colorbar with temperature scale and units (°C, NUC, RAW, W/m²sr)
- Cursor temperature readout (mouse hover)
- Click to place persistent markers with temperature labels
- Right-click to clear markers
- Min/max/mean statistics in status bar
- Colormap selector (inferno, hot, plasma, magma, viridis, gray)
- Percentile normalization (handles dead pixels)

---

## API Design Philosophy

**"What you set is what you get":**
- `cam.resolution = (320, 256)` → frame shape is (256, 320)
- `cam.grab()` returns Celsius in RT mode, not raw counts
- Headers are stripped automatically
- Buffer recording is one call: `cam.buffer_record()`

**String enums eliminate imports:**
```python
cam.calibration_mode = "RT"          # instead of reg.CalibrationMode.RT
cam.calibration_load(lens="50mm", temp=25)  # instead of index lookup
```

**Auto-handling of common issues:**
- Camera cooling down → auto-waits with progress
- Stale session from crash → auto-recovers
- Frame rate too high → warns with max value
- Wrong resolution → suggests nearest valid

**Sensible defaults on connect:**
- Bad pixel replacement ON
- Frame rate mode FIXED (adjustable during streaming)
- Test image OFF
- Frame grabber throttle removed

---

## Test Suite

| Category | Count | Requires Camera |
|---|---|---|
| GVCP protocol (packet format, ACK validation, retries) | 29 | No |
| GVSP frame assembly (pre-allocated buffers, ordering) | 14 | No |
| Register addresses and enum values | 12 | No |
| Camera init, discovery mock, enum resolution | 53 | No |
| Resolution validation | 12 | No |
| Calibration file parsing | 6 | No |
| **Total unit tests** | **116** | **No** |
| Discovery, connection, properties | 13 | Yes |
| Streaming, grab, acquire | 6 | Yes |
| Buffer configure, record, download | 12 | Yes |
| Calibration, diagnostics, resolution, RT conversion | 24 | Yes |
| Full workflow end-to-end | 2 | Yes |
| **Total hardware tests** | **57** | **Yes** |

CI runs unit tests on Python 3.10-3.13, Windows + Linux via GitHub Actions.

---

## Technologies & References

- **GigE Vision 1.2 specification** — GVCP/GVSP protocols
- **GenICam Standard Features Naming Convention** — register naming
- **aravis** (open-source GigE Vision library) — studied for GVCP ACK handling, GVSP resend architecture, pre-allocated buffers
- **fasthcc** — Telops HCC file format reader/writer (companion package)
- **TelopsToolbox** — reference for temperature conversion formula
- **HCC Header Reference v13.4** — per-frame DataExp/DataOffset fields

---

## Current State

The package is fully functional with 116 unit tests and 57 hardware tests. It supports:

- Auto-discovery, connect/disconnect with context manager
- All camera settings as properties with string enum support
- Live streaming (up to ~760 fps theoretical at full resolution)
- Buffer recording at up to 95k fps (64×4 at 5 µs integration time)
- Buffer download at ~270 fps / 45 MB/s
- Multi-sequence recording with automatic MOI triggering
- Calibration block selection by lens name and target temperature
- RT mode auto-conversion to Celsius
- 13 temperature sensors, voltage/current monitoring
- NUC trigger, bad pixel replacement, image flip
- Resolution validation with clear error messages
- Live thermal viewer with colorbar, cursor readout, and markers
- Robust connection handling (stale sessions, cooling down, control loss)
- CLI tools: discover, info, grab, live, setup
