# pyTelops — Development Report

## Overview

**pyTelops** is a pure-Python driver for Telops FAST-series MWIR thermal cameras over GigE Vision. It communicates directly via GVCP/GVSP protocols over UDP — no vendor SDK, no compiled extensions.

- **Camera**: Telops FAST M3k — InSb MWIR detector, 320×256, 25 mK sensitivity, 16 GB internal buffer
- **Connection**: GigE Vision 1.2 over Ethernet (link-local 169.254.x.x)
- **Repository**: https://github.com/ladisk/pyTelops
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

As of v0.2.0 the GigE Vision protocol layer (GVCP, GVSP, standard
register constants, GenICam XML download helper) lives in a separate
package, **pyGigEVision**, which pyTelops depends on. pyTelops keeps
only the Telops-specific parts (vendor register map, calibration
loading, RT-mode Celsius conversion, 16 GB onboard buffer recording,
2-row image header handling). Lorenzo's open-flir driver will rebase
onto the same pyGigEVision base.

```
┌──────────────────────────────────────┐
│  pyTelops.Camera (camera.py)         │  User-facing API: properties,
│  - Properties, context manager       │  context manager, auto-discovery
│  - Buffer recording, calibration     │  Vendor-specific behaviour
│  - RT-mode Celsius, header strip     │
├──────────────────────────────────────┤
│  pyTelops.registers                  │  Telops-specific addresses
│  16 IntEnum classes                  │  (Width, ExposureTime, MemoryBuffer
│  ~70 vendor register addresses       │   regs, calibration regs, ...)
├──────────────────────────────────────┤   ← pyTelops
                                          ─── pyGigEVision (separate package)
├──────────────┬───────────────────────┤
│  GVCPClient  │  GVSPReceiver         │
│  (gvcp.py)   │  (gvsp.py)            │
│  Control     │  Streaming            │
│  UDP :3956   │  UDP dynamic port     │
├──────────────┴───────────────────────┤
│  pyGigEVision.standard               │  GigE Vision spec registers
│  bootstrap, fetch_genicam_xml        │  (CCP, heartbeat, SC channel,
│                                      │   FIRST_URL — same on every camera)
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

### Phase 13: Continuous Acquisition API

`grab()` and `acquire(N)` are convenient for single-shot and batch use, but they start and stop the stream on every call. Each cycle is ~5 register writes plus a GVSP receiver thread spawn — roughly 50-200 ms of overhead per call. For live processing loops (LDAQ acquisition sources, live matplotlib plots, real-time ML) this overhead is fatal.

The new public API decouples the stream lifecycle from the frame pull:

```python
cam.acquisition_start()       # set up stream + write REG_ACQUISITION_START
with cam.acquisition():       # OR context manager (exception-safe)
    for _ in range(N):
        frame = cam.read_frame(timeout=0.0)  # non-blocking pull
cam.acquisition_stop()
```

- `acquisition_start/stop` — idempotent lifecycle primitives
- `acquisition()` — context manager wrapping start/stop
- `read_frame(timeout, strip_header, convert, latest)` — non-blocking frame pull
- `is_acquiring` — read-only state flag
- `grab()` and `acquire()` refactored to use the new primitives internally (same external behavior)

`gui.py` (the Tkinter live viewer) migrated off private register access (`cam._gvcp.write_reg(REG_ACQUISITION_START, 1)` → `cam.acquisition_start()`) and now uses only public API.

**Reviewer-found bug fix:** in the old `grab()`/`acquire()` cleanup, if the `REG_ACQUISITION_START` write raised `GVCPError` after `start_stream()` had already bound the GVSP socket, the socket was leaked. The new implementation wraps `acquisition_start()` inside the try block so cleanup always runs. Regression test included.

### Phase 14: Bounded-latency live display (`latest=True`)

When a live processing loop is slower than the camera frame rate (common with matplotlib redraws, Qt event loop, heavy per-frame computation), frames accumulate in pyTelops's internal GVSP queue. The loop processes them in order, each one staler than the last, and end-to-end latency grows every second.

`read_frame(latest=True)` drains the queue and returns only the most recent frame, discarding intermediate ones. Bounds latency to one frame period + one render regardless of consumer speed.

```python
with cam.acquisition():
    while running:
        frame = cam.read_frame(timeout=0.1, latest=True)
        if frame is not None:
            process_and_display(frame)
```

The trade-off is documented: `latest=True` for live display, `latest=False` (the default) for measurement / logging where every frame matters.

### Phase 15: Configurable `packet_delay`

At the camera's default packet delay of 0, all ~113 packets of a frame are sent back-to-back at GigE line rate — a ~1.4 ms burst. Any host-side hiccup (Python GC, matplotlib redraw, Qt event loop jitter) during the burst can overflow the kernel UDP receive queue and drop packets. At high frame rates, the `"Frame N: X/113 packets unrecoverable"` warnings become a regular occurrence.

New `cam.packet_delay` property maps to `REG_SC_PACKET_DELAY` (8 ns ticks). Setting it to `1000` spreads the 113-packet burst over ~2 ms instead of 1.4 ms, giving the host significantly more slack without measurably affecting frame rate up to ~400 fps.

**Backward compatibility:** an internal `_packet_delay_override` flag tracks whether the user has set a value. `start_stream()` still forces the register to 0 by default (preserving the original "maximum throughput" behavior), and only uses the override when the user has explicitly set one. The override persists across stream restarts.

### Phase 16: `roi_offset` client-side validation

The `roi_offset` setter previously wrote raw values to the camera without checking alignment. Passing an offset that wasn't a multiple of `WIDTH_STEP` (64) or `HEIGHT_STEP` (4), or that pushed the subwindow outside the sensor, returned a cryptic `GENERIC_ERROR` from the camera's register write — frustrating to debug.

The setter now validates client-side: non-negative, proper step alignment, and `x + width <= WIDTH_MAX` / `y + height <= HEIGHT_MAX`. Raises `ValueError` with a specific, actionable message naming the offending value and listing the valid set. Mirrors the existing `_validate_resolution()` defensive pattern.

### Phase 17: `buffer_clear` auto-reapply

The camera's `REG_MEMORY_BUFFER_CLEAR_ALL` register wipes partition configuration in addition to recorded data. After a bare `buffer_clear()`, a subsequent `buffer_record()` fired into an unconfigured buffer and the download silently hung. The natural `record → clear → record → download` workflow failed the second time through.

`buffer_configure()` now stores its kwargs on the Camera instance. `buffer_clear()` automatically re-applies them (with a 100 ms settle delay) so the partition state is restored after the clear. If `buffer_configure()` was never called in the session, `buffer_clear()` is still a plain single-register write.

### Phase 18: Split out pyGigEVision (v0.2.0, 2026-05-13)

**Why.** With Lorenzo (LolloCappo) starting an open-flir driver from the same protocol primitives, the GigE Vision protocol code in pyTelops became something both drivers needed. Rather than have each vendor driver vendorize a copy of `gvcp.py`/`gvsp.py`, we extracted them into a standalone package that both depend on. The market survey from April 2026 (see `py-gigevision-opportunity.md`) confirmed no other pure-Python, no-SDK, no-GenTL GigE Vision library exists, so the split is also a small contribution to the broader ecosystem (eventually).

**What moved (to `pyGigEVision`).** Everything that worked unchanged on any GigE Vision camera, regardless of vendor:

- `gvcp.py` — discovery, register read/write, memory access, heartbeat (lifted verbatim — only logger name changes and one Telops-named comment genericized).
- `gvsp.py` — packet reassembly, gap detection, resend, payload-size auto-detection (already had a `byte_order` parameter for non-Telops cameras).
- GigE Vision spec register addresses — `REG_CCP`, `REG_HEARTBEAT_TIMEOUT`, `REG_FIRST_URL`, `REG_SC_HOST_PORT`, `REG_SC_PACKET_SIZE`, `REG_SC_PACKET_DELAY`, `REG_SC_DEST_ADDR`, plus the `SC_PACKET_SIZE_MASK` / `SC_SCPS_*` flag bits — collected into a new `pyGigEVision.standard` module.
- New `pyGigEVision.genicam` — fetches the GenICam XML descriptor from a connected camera (the `Local:foo.xml;0xADDR;0xSIZE` URL parsing + zip decompression that every driver was inlining).
- New `pyGigEVision.bootstrap` — convenience helper that opens GVCP, takes control privilege, starts the heartbeat, and returns the GenICam XML in one call.

**What stayed (in `pyTelops`).** Everything Telops-specific:

- `Camera` (the user-facing class) — connect with cooldown wait, calibration loading from `.tsco`/`.tsbl` files, RT-mode Celsius conversion via per-frame DataExp/DataOffset, 16 GB onboard buffer record/download, 2-row image header strip, Telops resolution constraints (64-step width, 4-step height), Telops manufacturer filter on `discover()`.
- `registers.py` — Telops-specific addresses (Width, Height, ExposureTime, MemoryBuffer registers, Calibration registers, all 16 IntEnums). The standard SC block was removed in this phase; everything else is unchanged.
- `cli.py`, `gui.py` — unchanged.

**API impact (back-compat preserved).** `pyTelops`'s public surface is unchanged for users:

- `from pyTelops import Camera` — still works the same.
- `from pyTelops import GVCPClient, GVCPError` — still works; `__init__.py` re-exports them from `pyGigEVision`. `pyTelops.GVCPClient is pyGigEVision.GVCPClient` (same class object).
- `from pyTelops.gvcp import GVCPClient` — broken (the file is gone). Anyone importing internal modules directly needs to update to `from pyGigEVision import GVCPClient`. No production user is known to do this.
- `pyTelops.registers.REG_SC_*` — broken (the constants moved). Internal callers in `camera.py` were already updated to import from `pyGigEVision.standard`. External callers should do the same.

**Why no `BaseCamera` class in pyGigEVision.** The standard rule is to extract abstractions at three implementations, not one. With only `Camera` (Telops) as a real example and open-flir not yet shipping, designing a `BaseCamera` now would almost certainly bake in Telops-specific assumptions (cooldown wait, 2-row header) that don't generalize. Phase 2 of the pyGigEVision design is intentionally undecided — `BaseCamera` will only land if the same lifecycle pattern actually repeats across Telops, FLIR, and a third vendor.

**Versioning and dependency.** `pyTelops 0.2.0` declares `pyGigEVision >= 0.1.0` as a dependency, pinned to the private `git+ssh://git@github.com/ladisk/pyGigEVision.git@v0.1.0` URL while both repos are private. Switches to a normal PyPI version pin when both go public.

**CI.** `pyGigEVision` has its own GitHub Actions matrix (Python 3.10–3.13 × Ubuntu + Windows). pyTelops's automated tests workflow is currently `workflow_dispatch`-only — automated CI on private repos with private git+ssh dependencies needs deploy keys or PAT secrets, not yet wired up.

**Spec and plan.** Full design and implementation plan are committed in this repo at `docs/superpowers/specs/2026-05-13-pygigevision-extraction-design.md` and `docs/superpowers/plans/2026-05-13-pygigevision-extraction.md`.

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

GVCP and GVSP protocol tests now live in **pyGigEVision**'s own suite —
they were lifted with the code in v0.2.0. The numbers below are pyTelops's
remaining vendor-layer coverage.

| Category | Count | Requires Camera |
|---|---|---|
| Telops register addresses and enum values | 11 | No |
| Camera init, discovery mock, enum resolution | 53 | No |
| Resolution validation | 12 | No |
| Calibration file parsing | 6 | No |
| Continuous acquisition API (start/stop, context manager, read_frame, grab/acquire refactor, leak regression) | 18 | No |
| `read_frame(latest=True)` drain behavior | 4 | No |
| `packet_delay` property (getter/setter, override persistence, start_stream backward compat) | 9 | No |
| `roi_offset` client-side validation | 5 | No |
| `buffer_clear` auto-reapply | 2 | No |
| **Total unit tests (pyTelops)** | **127** | **No** |
| _Plus, in pyGigEVision: GVCP (29) + GVSP (15) + standard regs (3) + GenICam XML helper (4) + bootstrap (1)_ | _52_ | _No_ |
| Discovery, connection, properties | 13 | Yes |
| Streaming, grab, acquire | 6 | Yes |
| Buffer configure, record, download | 12 | Yes |
| Calibration, diagnostics, resolution, RT conversion | 24 | Yes |
| Full workflow end-to-end | 2 | Yes |
| **Total hardware tests** | **57** | **Yes** |

pyGigEVision's tests auto-run on every push and PR via its own GitHub
Actions matrix (Python 3.10–3.13 × Ubuntu + Windows). pyTelops's tests
are wired up but currently `workflow_dispatch`-only — automated CI on a
private repo with a private `git+ssh` dependency needs deploy keys or a
PAT secret in the workflow, not yet configured.

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

Currently at v0.2.0 (depends on pyGigEVision v0.1.0). The package is
fully functional with 127 vendor-layer unit tests in pyTelops, 52
protocol-layer unit tests in pyGigEVision, and 57 hardware tests in
pyTelops. It supports:

- Auto-discovery, connect/disconnect with context manager
- All camera settings as properties with string enum support
- Live streaming (up to ~760 fps theoretical at full resolution)
- Continuous acquisition API (`acquisition_start/stop`, `acquisition()` context manager, `read_frame`) with bounded-latency `latest=True` mode for live displays
- Configurable `packet_delay` for host-side UDP buffer relief at high frame rates
- Buffer recording at up to 95k fps (64×4 at 5 µs integration time)
- Buffer download at ~270 fps / 45 MB/s, tunable `bitrate_mbps` for competing-load scenarios
- Multi-sequence recording with automatic MOI triggering
- `buffer_clear` auto-reapplies the last `buffer_configure` so the natural clear → record → download flow works
- Calibration block selection by lens name and target temperature
- RT mode auto-conversion to Celsius
- 13 temperature sensors, voltage/current monitoring
- NUC trigger, bad pixel replacement, image flip
- Resolution and `roi_offset` validation with clear client-side error messages
- Live thermal viewer with colorbar, cursor readout, and markers — uses only public acquisition API
- Robust connection handling (stale sessions, cooling down, control loss)
- CLI tools: discover, info, grab, live, setup
- Top-level `TROUBLESHOOTING.rst` covering firewall setup, `packets unrecoverable` warnings, growing-lag live displays, and buffer download failures under competing load
