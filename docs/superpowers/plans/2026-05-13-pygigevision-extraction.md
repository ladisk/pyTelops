# pyGigEVision Phase 1 Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lift the generic GigE Vision protocol code (GVCP, GVSP, standard registers, GenICam XML download) out of pyTelops into the standalone `pyGigEVision` package, then refactor pyTelops to depend on it. Both repos stay private.

**Architecture:** Three-phase work: (A) build pyGigEVision in `ladisk/pyGigEVision` from lifted pyTelops code, (B) migrate pyTelops imports onto pyGigEVision in `ladisk/pyTelops`, (C) write a private MD handoff note for Lorenzo. No `BaseCamera`, no CLI, no vendor mentions in pyGigEVision.

**Tech Stack:** Python 3.10+, hatchling, pytest, GitHub Actions, numpy. Pure-Python, no C extensions.

**Spec:** See `pyTelops/docs/superpowers/specs/2026-05-13-pygigevision-extraction-design.md`.

---

## Pre-flight

### Task 0: Confirm starting state

**Files:** none — verification only.

- [ ] **Step 1: Verify pyTelops repo is clean and on master**

```bash
cd /c/Users/jasas/Work/OpenSource/pyTelops && git status && git log --oneline -1
```

Expected: `On branch master` ... `nothing to commit, working tree clean` and the most recent commit is `Add pyGigEVision Phase 1 extraction design`.

- [ ] **Step 2: Verify pyGigEVision repo is clean and on main**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && git status
```

Expected: `On branch main` ... `nothing to commit, working tree clean` and tracking `origin/main`.

- [ ] **Step 3: Verify pyTelops's venv is active and locate it**

```bash
where python
```

Expected: a path under a pyTelops venv (e.g., `...\pyTelops\.venv\Scripts\python.exe`). If not, activate before continuing.

- [ ] **Step 4: Verify pyTelops's full test suite is currently green (baseline)**

```bash
cd /c/Users/jasas/Work/OpenSource/pyTelops && python -m pytest -q
```

Expected: 161 unit tests pass, 57 hardware tests skipped (without `--hardware` flag). 0 failures. This is the baseline we must not regress.

---

## Phase A: Build pyGigEVision

Work happens in `C:\Users\jasas\Work\OpenSource\pyGigEVision\` on `main`.

### Task 1: Update pyproject.toml and create tests/ skeleton

**Files:**
- Modify: `pyGigEVision/pyproject.toml`
- Create: `pyGigEVision/tests/__init__.py`
- Create: `pyGigEVision/tests/conftest.py`

- [ ] **Step 1: Replace pyproject.toml with full project metadata**

Overwrite `pyGigEVision/pyproject.toml` with:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "pyGigEVision"
version = "0.0.1"
description = "Pure-Python implementation of the GigE Vision protocol (GVCP + GVSP)."
license = "MIT"
requires-python = ">=3.10"
readme = "PYPI_README.rst"
authors = [
    { name = "jasasonc" },
]
maintainers = [
    { name = "jasasonc" },
    { name = "LolloCappo" },
    { name = "jankoslavic" },
]
keywords = ["gige", "gigevision", "machine vision", "camera", "industrial"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Science/Research",
    "License :: OSI Approved :: MIT License",
    "Operating System :: Microsoft :: Windows",
    "Operating System :: POSIX :: Linux",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Topic :: Scientific/Engineering",
    "Topic :: System :: Hardware :: Hardware Drivers",
]
dependencies = [
    "numpy>=1.20",
]

[project.optional-dependencies]
test = ["pytest", "pytest-cov"]
dev = ["pytest", "pytest-cov"]

[project.urls]
Homepage = "https://github.com/ladisk/pyGigEVision"
Repository = "https://github.com/ladisk/pyGigEVision"
Issues = "https://github.com/ladisk/pyGigEVision/issues"

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.hatch.build.targets.sdist]
include = [
    "src/pyGigEVision/",
]
exclude = [
    "README.rst",
]

[tool.hatch.build.targets.wheel]
packages = ["src/pyGigEVision"]
```

Note: version stays at `0.0.1` until Task 9. No `tqdm` dep (per spec).

- [ ] **Step 2: Create empty tests/__init__.py**

Create `pyGigEVision/tests/__init__.py` as an empty file.

- [ ] **Step 3: Create tests/conftest.py**

Create `pyGigEVision/tests/conftest.py`:

```python
"""Test configuration. pyGigEVision has no hardware tests — protocol is
vendor-agnostic and hardware coverage lives in vendor drivers."""
```

- [ ] **Step 4: Verify package still imports**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && pip install -e . --quiet && python -c "import pyGigEVision; print(pyGigEVision.__version__)"
```

Expected: `0.0.1`.

- [ ] **Step 5: Commit**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && git add pyproject.toml tests/__init__.py tests/conftest.py && git commit -m "Set up project metadata and tests directory"
```

---

### Task 2: Create standard.py with all GigE Vision spec registers

**Files:**
- Create: `pyGigEVision/src/pyGigEVision/standard.py`
- Create: `pyGigEVision/tests/test_standard.py`

- [ ] **Step 1: Write the failing test**

Create `pyGigEVision/tests/test_standard.py`:

```python
"""Sanity tests for GigE Vision standard register addresses."""

from pyGigEVision import standard as std


def test_bootstrap_addresses():
    assert std.REG_CCP == 0x0A00
    assert std.REG_HEARTBEAT_TIMEOUT == 0x0938
    assert std.REG_FIRST_URL == 0x0200


def test_stream_channel_addresses():
    assert std.REG_SC_HOST_PORT == 0x0D00
    assert std.REG_SC_PACKET_SIZE == 0x0D04
    assert std.REG_SC_PACKET_DELAY == 0x0D08
    assert std.REG_SC_DEST_ADDR == 0x0D18


def test_packet_size_flag_layout():
    # Bits 15:2 = packet size; bit 1 = do-not-fragment; bit 0 = test packet
    assert std.SC_PACKET_SIZE_MASK == 0xFFFC
    assert std.SC_SCPS_DO_NOT_FRAGMENT == 1 << 1
    assert std.SC_SCPS_FIRE_TEST_PACKET == 1 << 0
```

- [ ] **Step 2: Run test, verify it fails**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && python -m pytest tests/test_standard.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'pyGigEVision.standard'` or similar.

- [ ] **Step 3: Create standard.py**

Create `pyGigEVision/src/pyGigEVision/standard.py`:

```python
"""GigE Vision standard register addresses.

Defined by the GigE Vision specification — same on every compliant
camera regardless of vendor. Vendor-specific registers (Width, Height,
ExposureTime, etc.) live in each vendor driver, derived from the
camera's GenICam XML.
"""

# ============================================================
# Bootstrap registers (GigE Vision spec section: device control)
# ============================================================
REG_CCP = 0x0A00              # Control Channel Privilege
REG_HEARTBEAT_TIMEOUT = 0x0938
REG_FIRST_URL = 0x0200        # Location of GenICam XML descriptor URL

# ============================================================
# Stream Channel 0 registers (GigE Vision spec)
# ============================================================
REG_SC_HOST_PORT = 0x0D00
REG_SC_PACKET_SIZE = 0x0D04
# REG_SC_PACKET_SIZE layout:
#   Bits 15:2 — packet size in bytes
#   Bit 1     — GevSCPSDoNotFragment (1=don't fragment, 0=allow)
#   Bit 0     — GevSCPSFireTestPacket (write-only trigger)
SC_PACKET_SIZE_MASK = 0xFFFC
SC_SCPS_DO_NOT_FRAGMENT = 1 << 1
SC_SCPS_FIRE_TEST_PACKET = 1 << 0
REG_SC_PACKET_DELAY = 0x0D08
REG_SC_DEST_ADDR = 0x0D18
```

- [ ] **Step 4: Run test, verify it passes**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && python -m pytest tests/test_standard.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && git add src/pyGigEVision/standard.py tests/test_standard.py && git commit -m "Add standard.py with GigE Vision spec registers"
```

---

### Task 3: Lift gvcp.py from pyTelops

**Files:**
- Create: `pyGigEVision/src/pyGigEVision/gvcp.py`
- Create: `pyGigEVision/tests/test_gvcp.py`

- [ ] **Step 1: Copy gvcp.py verbatim**

```bash
cp /c/Users/jasas/Work/OpenSource/pyTelops/pyTelops/gvcp.py /c/Users/jasas/Work/OpenSource/pyGigEVision/src/pyGigEVision/gvcp.py
```

- [ ] **Step 2: Remove the inlined bootstrap register constants from gvcp.py**

In `pyGigEVision/src/pyGigEVision/gvcp.py`, delete these lines (currently around lines 35–38):

```python
# Bootstrap registers (GigE Vision standard, same on all cameras)
REG_CCP = 0x0A00
REG_HEARTBEAT_TIMEOUT = 0x0938
REG_FIRST_URL = 0x0200
```

Replace with an import from standard.py at the top of the file (after existing imports):

```python
from .standard import REG_CCP, REG_HEARTBEAT_TIMEOUT, REG_FIRST_URL
```

- [ ] **Step 3: Verify the file uses logging via `__name__`**

The lifted file already does `logger = logging.getLogger(__name__)`. When imported as `pyGigEVision.gvcp`, the logger name automatically becomes `pyGigEVision.gvcp`. No code change needed.

- [ ] **Step 4: Copy test_gvcp.py and update its imports**

```bash
cp /c/Users/jasas/Work/OpenSource/pyTelops/tests/test_gvcp.py /c/Users/jasas/Work/OpenSource/pyGigEVision/tests/test_gvcp.py
```

Then in the new file, replace all occurrences of `pyTelops.gvcp` with `pyGigEVision.gvcp`. There are three import sites — verify with:

```bash
grep -n "pyTelops" /c/Users/jasas/Work/OpenSource/pyGigEVision/tests/test_gvcp.py
```

Expected after replacement: no matches.

- [ ] **Step 5: Run test_gvcp.py**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && python -m pytest tests/test_gvcp.py -v
```

Expected: all tests pass (same count as the original pyTelops test_gvcp.py — should be ~30+ tests). If anything fails, the lift was not verbatim — diff against the source and fix.

- [ ] **Step 6: Commit**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && git add src/pyGigEVision/gvcp.py tests/test_gvcp.py && git commit -m "Lift gvcp.py from pyTelops"
```

---

### Task 4: Lift gvsp.py from pyTelops

**Files:**
- Create: `pyGigEVision/src/pyGigEVision/gvsp.py`
- Create: `pyGigEVision/tests/test_gvsp.py`

- [ ] **Step 1: Copy gvsp.py verbatim**

```bash
cp /c/Users/jasas/Work/OpenSource/pyTelops/pyTelops/gvsp.py /c/Users/jasas/Work/OpenSource/pyGigEVision/src/pyGigEVision/gvsp.py
```

- [ ] **Step 2: Verify gvsp.py has no bootstrap-register inlines and logger uses `__name__`**

```bash
grep -n "REG_CCP\|REG_HEARTBEAT\|REG_FIRST_URL\|logger = logging" /c/Users/jasas/Work/OpenSource/pyGigEVision/src/pyGigEVision/gvsp.py
```

Expected: only `logger = logging.getLogger(__name__)` shows. No bootstrap reg references (gvsp doesn't use them). No code change needed.

- [ ] **Step 3: Copy test_gvsp.py and update imports**

```bash
cp /c/Users/jasas/Work/OpenSource/pyTelops/tests/test_gvsp.py /c/Users/jasas/Work/OpenSource/pyGigEVision/tests/test_gvsp.py
```

In the new file, replace `pyTelops.gvsp` with `pyGigEVision.gvsp`. Verify:

```bash
grep -n "pyTelops" /c/Users/jasas/Work/OpenSource/pyGigEVision/tests/test_gvsp.py
```

Expected: no matches.

- [ ] **Step 4: Run test_gvsp.py**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && python -m pytest tests/test_gvsp.py -v
```

Expected: all tests pass (matches original pyTelops test_gvsp.py count).

- [ ] **Step 5: Commit**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && git add src/pyGigEVision/gvsp.py tests/test_gvsp.py && git commit -m "Lift gvsp.py from pyTelops"
```

---

### Task 5: Create genicam.py — GenICam XML download helper

**Files:**
- Create: `pyGigEVision/src/pyGigEVision/genicam.py`
- Create: `pyGigEVision/tests/test_genicam.py`

This module wraps the standard "read URL at REG_FIRST_URL, parse `Local:<filename>;<addr>;<size>`, download, decompress if zipped" pattern that every GigE Vision driver needs to do during boot. Logic exists today inlined in pyTelops and in the FLIR driver guide step 2.

- [ ] **Step 1: Write the failing test**

Create `pyGigEVision/tests/test_genicam.py`:

```python
"""Tests for fetch_genicam_xml — mocks GVCPClient.read_mem."""

import io
import zipfile
from unittest.mock import MagicMock

import pytest

from pyGigEVision.genicam import fetch_genicam_xml, parse_first_url


def test_parse_first_url_plain():
    url = b"Local:cameralib.xml;0x10000;0x4000\x00" + b"\x00" * 470
    filename, addr, size = parse_first_url(url)
    assert filename == "cameralib.xml"
    assert addr == 0x10000
    assert size == 0x4000


def test_parse_first_url_zipped():
    url = b"Local:cameralib.zip;0x20000;0x1000\x00" + b"\x00" * 470
    filename, addr, size = parse_first_url(url)
    assert filename == "cameralib.zip"
    assert addr == 0x20000
    assert size == 0x1000


def test_fetch_genicam_xml_plain():
    raw_xml = b"<RegisterDescription>fake xml</RegisterDescription>"
    url_bytes = (b"Local:cam.xml;0x10000;%d\x00" % len(raw_xml)).ljust(512, b"\x00")
    client = MagicMock()
    # First read_mem(0x0200, 512) returns the URL; second returns the XML
    client.read_mem.side_effect = [url_bytes, raw_xml]
    xml, filename = fetch_genicam_xml(client)
    assert xml == raw_xml
    assert filename == "cam.xml"


def test_fetch_genicam_xml_zipped():
    inner_xml = b"<RegisterDescription>real xml</RegisterDescription>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("cam.xml", inner_xml)
    zipped = buf.getvalue()
    url_bytes = (b"Local:cam.zip;0x20000;%d\x00" % len(zipped)).ljust(512, b"\x00")
    client = MagicMock()
    client.read_mem.side_effect = [url_bytes, zipped]
    xml, filename = fetch_genicam_xml(client)
    assert xml == inner_xml
    assert filename == "cam.xml"
```

- [ ] **Step 2: Run test, verify it fails**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && python -m pytest tests/test_genicam.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'pyGigEVision.genicam'`.

- [ ] **Step 3: Create genicam.py**

Create `pyGigEVision/src/pyGigEVision/genicam.py`:

```python
"""GenICam XML descriptor download helper.

Every GigE Vision camera stores its register description XML in on-board
memory. The location URL is at bootstrap register 0x0200 (REG_FIRST_URL)
and looks like::

    Local:cameralib.xml;0xADDR;0xSIZE

This module reads the URL, downloads the bytes, and decompresses if the
descriptor is zipped. Parsing the XML itself is left to the vendor driver
(register naming conventions differ enough across vendors that a generic
parser is not worth the abstraction).
"""

import io
import logging
import zipfile

from .standard import REG_FIRST_URL

logger = logging.getLogger(__name__)


def parse_first_url(url_bytes):
    """Parse the bytes read from REG_FIRST_URL into (filename, addr, size).

    Args:
        url_bytes: Raw bytes from ``client.read_mem(REG_FIRST_URL, 512)``.

    Returns:
        Tuple of (filename: str, addr: int, size: int).

    Raises:
        ValueError: If the URL string cannot be parsed.
    """
    url = url_bytes.split(b"\x00", 1)[0].decode("ascii")
    parts = url.split(";")
    if len(parts) < 3:
        raise ValueError(f"Malformed FIRST_URL: {url!r}")
    filename = parts[0].split(":")[-1]
    addr = int(parts[1], 0)
    size = int(parts[2], 0)
    return filename, addr, size


def fetch_genicam_xml(client):
    """Download the GenICam XML descriptor from a connected camera.

    Args:
        client: An open ``GVCPClient`` with control privilege.

    Returns:
        Tuple of (xml_bytes: bytes, filename: str). If the on-camera
        descriptor was zipped, ``xml_bytes`` is the decompressed XML and
        ``filename`` is the .xml entry name from the zip.
    """
    url_bytes = client.read_mem(REG_FIRST_URL, 512)
    filename, addr, size = parse_first_url(url_bytes)
    logger.info("Fetching GenICam descriptor: %s (addr=0x%X, %d bytes)",
                filename, addr, size)
    data = client.read_mem(addr, size)

    if filename.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml_name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
            return zf.read(xml_name), xml_name

    return data, filename
```

- [ ] **Step 4: Run test, verify it passes**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && python -m pytest tests/test_genicam.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && git add src/pyGigEVision/genicam.py tests/test_genicam.py && git commit -m "Add genicam.py XML download helper"
```

---

### Task 6: Create bootstrap.py — connect convenience helper

**Files:**
- Create: `pyGigEVision/src/pyGigEVision/bootstrap.py`
- Create: `pyGigEVision/tests/test_bootstrap.py`

Optional convenience: `bootstrap(camera_ip)` opens GVCP, sets CCP (control privilege), starts heartbeat, and returns `(client, xml_bytes)`. ~30 lines. Vendor drivers can use it or roll their own.

- [ ] **Step 1: Write the failing test**

Create `pyGigEVision/tests/test_bootstrap.py`:

```python
"""Tests for bootstrap() helper — uses a fake GVCPClient."""

from unittest.mock import MagicMock, patch

from pyGigEVision import bootstrap as boot_mod
from pyGigEVision.standard import REG_CCP, REG_HEARTBEAT_TIMEOUT


def test_bootstrap_writes_ccp_and_heartbeat_then_fetches_xml():
    fake_client = MagicMock()
    raw_xml = b"<RegisterDescription/>"
    url = (b"Local:cam.xml;0x10000;%d\x00" % len(raw_xml)).ljust(512, b"\x00")
    fake_client.read_mem.side_effect = [url, raw_xml]

    with patch.object(boot_mod, "GVCPClient", return_value=fake_client) as gv_cls:
        client, xml = boot_mod.bootstrap("169.254.1.1")

    gv_cls.assert_called_once_with("169.254.1.1")
    fake_client.connect.assert_called_once()
    fake_client.write_reg.assert_any_call(REG_CCP, 0x00000002)
    fake_client.write_reg.assert_any_call(REG_HEARTBEAT_TIMEOUT, 3000)
    assert xml == raw_xml
    assert client is fake_client
```

- [ ] **Step 2: Run test, verify it fails**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && python -m pytest tests/test_bootstrap.py -v
```

Expected: FAIL with `ImportError: cannot import name 'bootstrap'` or `ModuleNotFoundError`.

- [ ] **Step 3: Create bootstrap.py**

Create `pyGigEVision/src/pyGigEVision/bootstrap.py`:

```python
"""Convenience helper to bring a GigE Vision camera up to a usable state.

Performs the standard boot sequence shared by every GigE Vision driver:
acquire control privilege, start heartbeat keepalive, fetch the GenICam
XML descriptor. Vendor drivers use this to skip boilerplate, or roll
their own if they need finer control.
"""

from .gvcp import GVCPClient
from .genicam import fetch_genicam_xml
from .standard import REG_CCP, REG_HEARTBEAT_TIMEOUT

# CCP value 2 = exclusive control access
_CCP_EXCLUSIVE = 0x00000002

# 3 second heartbeat timeout — matches pyTelops default
_DEFAULT_HEARTBEAT_MS = 3000


def bootstrap(camera_ip, heartbeat_ms=_DEFAULT_HEARTBEAT_MS):
    """Connect to a camera, take control, fetch its GenICam XML.

    Args:
        camera_ip: IPv4 address of the target camera.
        heartbeat_ms: Heartbeat timeout to write to the camera, in ms.

    Returns:
        Tuple of (client: GVCPClient, xml_bytes: bytes). The client is
        connected and holds exclusive control privilege; the caller is
        responsible for ``client.close()`` when done.
    """
    client = GVCPClient(camera_ip)
    client.connect()
    client.write_reg(REG_CCP, _CCP_EXCLUSIVE)
    client.write_reg(REG_HEARTBEAT_TIMEOUT, heartbeat_ms)
    xml, _filename = fetch_genicam_xml(client)
    return client, xml
```

- [ ] **Step 4: Run test, verify it passes**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && python -m pytest tests/test_bootstrap.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && git add src/pyGigEVision/bootstrap.py tests/test_bootstrap.py && git commit -m "Add bootstrap() helper for standard camera boot sequence"
```

---

### Task 7: Update __init__.py and full-suite smoke test

**Files:**
- Modify: `pyGigEVision/src/pyGigEVision/__init__.py`

- [ ] **Step 1: Replace __init__.py**

Overwrite `pyGigEVision/src/pyGigEVision/__init__.py`:

```python
"""pyGigEVision — pure-Python GigE Vision protocol library.

Provides GVCP (control), GVSP (streaming), GigE Vision standard register
constants, and helpers for fetching the GenICam XML descriptor from a
connected camera. Vendor-specific drivers (register maps, calibration,
image-format quirks) are built on top — pyGigEVision is the protocol
foundation.

Quickstart::

    from pyGigEVision import discover, bootstrap, GVSPReceiver

    cameras = discover()
    client, xml = bootstrap(cameras[0]["ip"])
    # ... configure registers, start GVSPReceiver, grab frames
"""

__version__ = "0.0.1"

from .gvcp import GVCPClient, GVCPError, discover
from .gvsp import GVSPReceiver
from .genicam import fetch_genicam_xml, parse_first_url
from .bootstrap import bootstrap

__all__ = [
    "__version__",
    "GVCPClient",
    "GVCPError",
    "GVSPReceiver",
    "discover",
    "fetch_genicam_xml",
    "parse_first_url",
    "bootstrap",
]
```

Note: `discover` is exposed as a top-level helper. It is currently `GVCPClient.discover` (a classmethod on the lifted gvcp.py); the line `from .gvcp import GVCPClient, GVCPError, discover` will fail unless gvcp.py also exposes a module-level `discover`. Verify with:

```bash
grep -n "^def discover\|^class GVCPClient" /c/Users/jasas/Work/OpenSource/pyGigEVision/src/pyGigEVision/gvcp.py
```

If `discover` is only a classmethod, replace the import line with:

```python
from .gvcp import GVCPClient, GVCPError
discover = GVCPClient.discover
```

- [ ] **Step 2: Run full pyGigEVision test suite**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && python -m pytest -v
```

Expected: all tests pass (test_gvcp + test_gvsp + test_standard + test_genicam + test_bootstrap, ~40+ total).

- [ ] **Step 3: Smoke-test the public API surface**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && python -c "from pyGigEVision import GVCPClient, GVCPError, GVSPReceiver, discover, fetch_genicam_xml, bootstrap; from pyGigEVision.standard import REG_CCP, REG_SC_PACKET_DELAY; print('OK')"
```

Expected: `OK`.

- [ ] **Step 4: Commit**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && git add src/pyGigEVision/__init__.py && git commit -m "Wire up public API in __init__.py"
```

---

### Task 8: Set up GitHub Actions CI

**Files:**
- Create: `pyGigEVision/.github/workflows/test.yml`

- [ ] **Step 1: Create the workflow**

Create `pyGigEVision/.github/workflows/test.yml`:

```yaml
name: tests

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest]
        python-version: ["3.10", "3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install
        run: pip install -e ".[test]"
      - name: Run tests
        run: pytest -v
```

- [ ] **Step 2: Commit and push**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && git add .github/workflows/test.yml && git commit -m "Add GitHub Actions CI matrix" && git push origin main
```

- [ ] **Step 3: Verify CI passes on GitHub**

Open https://github.com/ladisk/pyGigEVision/actions and confirm the latest run is green across all 8 matrix cells (4 Python × 2 OS). If any fail, fix locally and push again.

---

### Task 9: Bump to 0.1.0, tag, push

**Files:**
- Modify: `pyGigEVision/pyproject.toml`
- Modify: `pyGigEVision/src/pyGigEVision/__init__.py`

- [ ] **Step 1: Bump version in pyproject.toml**

Edit `pyGigEVision/pyproject.toml`: change `version = "0.0.1"` to `version = "0.1.0"`.

- [ ] **Step 2: Bump __version__**

Edit `pyGigEVision/src/pyGigEVision/__init__.py`: change `__version__ = "0.0.1"` to `__version__ = "0.1.0"`.

- [ ] **Step 3: Commit, tag, push**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && git add pyproject.toml src/pyGigEVision/__init__.py && git commit -m "Bump to v0.1.0" && git tag v0.1.0 && git push origin main && git push origin v0.1.0
```

- [ ] **Step 4: Verify tag is on GitHub**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && git ls-remote --tags origin
```

Expected: `refs/tags/v0.1.0` shows. **No PyPI upload** per spec — `0.0.1` placeholder stays on PyPI; `0.1.0` lives only on private GitHub.

---

## Phase B: Migrate pyTelops onto pyGigEVision

Work happens in `C:\Users\jasas\Work\OpenSource\pyTelops\` on `master`.

### Task 10: Add pyGigEVision dependency to pyTelops and install

**Files:**
- Modify: `pyTelops/pyproject.toml:31-34`

- [ ] **Step 1: Add the git+ssh dependency**

In `pyTelops/pyproject.toml`, change the `dependencies` block from:

```toml
dependencies = [
    "numpy>=1.20",
    "tqdm",
]
```

to:

```toml
dependencies = [
    "numpy>=1.20",
    "tqdm",
    "pyGigEVision @ git+ssh://git@github.com/ladisk/pyGigEVision.git@v0.1.0",
]
```

- [ ] **Step 2: Install pyGigEVision into pyTelops's venv**

```bash
cd /c/Users/jasas/Work/OpenSource/pyTelops && pip install -e ../pyGigEVision
```

(Editable local install for development convenience — equivalent to the git+ssh pin once pushed. Avoids hitting the network for every test cycle.)

- [ ] **Step 3: Verify it imports**

```bash
python -c "import pyGigEVision; print(pyGigEVision.__version__)"
```

Expected: `0.1.0`.

- [ ] **Step 4: Commit**

```bash
cd /c/Users/jasas/Work/OpenSource/pyTelops && git add pyproject.toml && git commit -m "Add pyGigEVision dependency"
```

---

### Task 11: Switch camera.py imports to pyGigEVision

**Files:**
- Modify: `pyTelops/pyTelops/camera.py:29-31` and call sites

- [ ] **Step 1: Replace the import block at lines 29-31**

In `pyTelops/pyTelops/camera.py`, replace:

```python
from .gvcp import GVCPClient, GVCPError, REG_HEARTBEAT_TIMEOUT
from .gvsp import GVSPReceiver
from . import registers as reg
```

with:

```python
from pyGigEVision import GVCPClient, GVCPError, GVSPReceiver
from pyGigEVision.standard import (
    REG_HEARTBEAT_TIMEOUT,
    REG_SC_HOST_PORT, REG_SC_PACKET_SIZE, REG_SC_PACKET_DELAY, REG_SC_DEST_ADDR,
    SC_PACKET_SIZE_MASK, SC_SCPS_DO_NOT_FRAGMENT,
)
from . import registers as reg
```

- [ ] **Step 2: Replace `reg.REG_SC_*` and `reg.SC_*` references with bare names**

In `pyTelops/pyTelops/camera.py`, perform these find-and-replace operations (these are the names now imported directly above instead of via `reg.`):

| Find | Replace |
|---|---|
| `reg.REG_SC_HOST_PORT` | `REG_SC_HOST_PORT` |
| `reg.REG_SC_PACKET_SIZE` | `REG_SC_PACKET_SIZE` |
| `reg.REG_SC_PACKET_DELAY` | `REG_SC_PACKET_DELAY` |
| `reg.REG_SC_DEST_ADDR` | `REG_SC_DEST_ADDR` |
| `reg.SC_PACKET_SIZE_MASK` | `SC_PACKET_SIZE_MASK` |
| `reg.SC_SCPS_DO_NOT_FRAGMENT` | `SC_SCPS_DO_NOT_FRAGMENT` |

Verify nothing was missed:

```bash
grep -n "reg.REG_SC\|reg.SC_PACKET\|reg.SC_SCPS" /c/Users/jasas/Work/OpenSource/pyTelops/pyTelops/camera.py
```

Expected: no matches.

- [ ] **Step 3: Verify all other `reg.*` references are Telops-specific (still valid)**

```bash
grep -on "reg\\.[A-Z_]*" /c/Users/jasas/Work/OpenSource/pyTelops/pyTelops/camera.py | sort -u
```

Expected: only Telops-specific names — `reg.REG_WIDTH`, `reg.REG_EXPOSURE_TIME`, `reg.CalibrationMode`, etc. No standard-spec names.

- [ ] **Step 4: Spot-check by importing camera.py**

```bash
cd /c/Users/jasas/Work/OpenSource/pyTelops && python -c "from pyTelops.camera import Camera; print('OK')"
```

Expected: `OK`. Will currently fail because `pyTelops/gvcp.py` and `pyTelops/gvsp.py` still exist as stale duplicates — that's fine, this is just an import-syntax check. Acceptable failures: `ImportError` from these stale files referencing things that moved. If you see `SyntaxError` or `NameError` in camera.py, fix that here.

- [ ] **Step 5: Commit**

```bash
cd /c/Users/jasas/Work/OpenSource/pyTelops && git add pyTelops/camera.py && git commit -m "Switch camera.py to import protocol primitives from pyGigEVision"
```

---

### Task 12: Update pyTelops/__init__.py to re-export from pyGigEVision

**Files:**
- Modify: `pyTelops/pyTelops/__init__.py:18`

- [ ] **Step 1: Replace the gvcp import line**

In `pyTelops/pyTelops/__init__.py`, change line 18 from:

```python
from .gvcp import GVCPClient, GVCPError
```

to:

```python
from pyGigEVision import GVCPClient, GVCPError
```

The rest of the file (registers imports, `discover`, enum re-exports) is unchanged.

- [ ] **Step 2: Verify the public surface still imports**

```bash
cd /c/Users/jasas/Work/OpenSource/pyTelops && python -c "from pyTelops import Camera, discover, GVCPClient, GVCPError, CalibrationMode; print('OK')"
```

Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
cd /c/Users/jasas/Work/OpenSource/pyTelops && git add pyTelops/__init__.py && git commit -m "Re-export GVCPClient/GVCPError from pyGigEVision"
```

---

### Task 13: Trim pyTelops/registers.py — remove standard SC registers

**Files:**
- Modify: `pyTelops/pyTelops/registers.py:11-24`

- [ ] **Step 1: Delete the Stream Channel 0 register block**

In `pyTelops/pyTelops/registers.py`, delete lines 11–24 (the `# Stream Channel 0 Registers (GigE Vision standard)` block). The file should now jump from the file docstring directly to the `# Image Format` block at line 26.

Verify the deleted constants are gone:

```bash
grep -n "REG_SC\|SC_PACKET_SIZE_MASK\|SC_SCPS" /c/Users/jasas/Work/OpenSource/pyTelops/pyTelops/registers.py
```

Expected: no matches.

- [ ] **Step 2: Verify Telops-specific content is intact**

```bash
grep -cn "^REG_\|^class.*IntEnum" /c/Users/jasas/Work/OpenSource/pyTelops/pyTelops/registers.py
```

Expected: a count similar to before minus the 6 standard constants we removed (originally ~80+ entries; should still be 70+).

- [ ] **Step 3: Commit**

```bash
cd /c/Users/jasas/Work/OpenSource/pyTelops && git add pyTelops/registers.py && git commit -m "Remove standard GigE Vision regs from pyTelops/registers.py"
```

---

### Task 14: Delete pyTelops/gvcp.py and pyTelops/gvsp.py

**Files:**
- Delete: `pyTelops/pyTelops/gvcp.py`
- Delete: `pyTelops/pyTelops/gvsp.py`

- [ ] **Step 1: Delete the files**

```bash
cd /c/Users/jasas/Work/OpenSource/pyTelops && rm pyTelops/gvcp.py pyTelops/gvsp.py
```

- [ ] **Step 2: Verify no surviving references**

```bash
grep -rn "from .gvcp\|from .gvsp\|from pyTelops.gvcp\|from pyTelops.gvsp" /c/Users/jasas/Work/OpenSource/pyTelops/pyTelops/
```

Expected: no matches.

- [ ] **Step 3: Quick import smoke test**

```bash
cd /c/Users/jasas/Work/OpenSource/pyTelops && python -c "from pyTelops import Camera; print('OK')"
```

Expected: `OK`. (Tests will catch deeper issues in Task 16.)

- [ ] **Step 4: Commit**

```bash
cd /c/Users/jasas/Work/OpenSource/pyTelops && git add -A pyTelops/ && git commit -m "Delete pyTelops/gvcp.py and gvsp.py (lifted to pyGigEVision)"
```

---

### Task 15: Update pyTelops test imports and remove moved tests

**Files:**
- Delete: `pyTelops/tests/test_gvcp.py`
- Delete: `pyTelops/tests/test_gvsp.py`
- Modify: `pyTelops/tests/test_camera.py:462`
- Modify: `pyTelops/tests/test_hardware.py:18`

- [ ] **Step 1: Delete the lifted test files**

```bash
cd /c/Users/jasas/Work/OpenSource/pyTelops && rm tests/test_gvcp.py tests/test_gvsp.py
```

- [ ] **Step 2: Update test_camera.py**

In `pyTelops/tests/test_camera.py`, change line 462 from:

```python
        from pyTelops.gvcp import GVCPError
```

to:

```python
        from pyGigEVision import GVCPError
```

- [ ] **Step 3: Update test_hardware.py**

In `pyTelops/tests/test_hardware.py`, change line 18 from:

```python
from pyTelops.gvcp import GVCPError
```

to:

```python
from pyGigEVision import GVCPError
```

- [ ] **Step 4: Verify no surviving pyTelops.gvcp / pyTelops.gvsp test imports**

```bash
grep -rn "pyTelops.gvcp\|pyTelops.gvsp" /c/Users/jasas/Work/OpenSource/pyTelops/tests/
```

Expected: no matches.

- [ ] **Step 5: Commit**

```bash
cd /c/Users/jasas/Work/OpenSource/pyTelops && git add -A tests/ && git commit -m "Update test imports for pyGigEVision migration"
```

---

### Task 16: Validation gate — full pyTelops test suite

**Files:** none — verification only.

- [ ] **Step 1: Run the unit test suite**

```bash
cd /c/Users/jasas/Work/OpenSource/pyTelops && python -m pytest -v
```

Expected: all unit tests pass (was 161 in pyTelops, now ~131 because test_gvcp.py + test_gvsp.py moved to pyGigEVision; their coverage is preserved over there). 0 failures. 57 hardware tests skipped.

If any test fails: do NOT proceed. Diagnose and fix in a new task before continuing — the gate exists to prevent shipping a broken migration. Most likely failure modes: a missed `reg.REG_SC_*` reference (search again with the grep from Task 11), or an `__init__.py` re-export gap.

- [ ] **Step 2: Run hardware tests if a Telops camera is connected**

```bash
cd /c/Users/jasas/Work/OpenSource/pyTelops && python -m pytest --hardware -v
```

Expected: 57 hardware tests pass. If no camera is available, **skip this step but flag it explicitly to the user before tagging 0.2.0** — the migration is unverified at the protocol-over-the-wire level until the hardware suite runs green.

- [ ] **Step 3: Run pyGigEVision's tests too (in pyTelops's venv) for full coverage**

```bash
cd /c/Users/jasas/Work/OpenSource/pyGigEVision && python -m pytest -v
```

Expected: all pyGigEVision tests pass (same as Task 7).

---

### Task 17: Bump pyTelops to 0.2.0 and tag

**Files:**
- Modify: `pyTelops/pyproject.toml:7`
- Modify: `pyTelops/pyTelops/__init__.py:15`

- [ ] **Step 1: Bump version in pyproject.toml**

In `pyTelops/pyproject.toml`, change `version = "0.1.0"` to `version = "0.2.0"`.

- [ ] **Step 2: Bump __version__**

In `pyTelops/pyTelops/__init__.py`, change `__version__ = "0.1.0"` to `__version__ = "0.2.0"`.

- [ ] **Step 3: Commit, tag, push**

```bash
cd /c/Users/jasas/Work/OpenSource/pyTelops && git add pyproject.toml pyTelops/__init__.py && git commit -m "Bump to v0.2.0 (depends on pyGigEVision)" && git tag v0.2.0 && git push origin master && git push origin v0.2.0
```

- [ ] **Step 4: Verify tag is on GitHub**

```bash
cd /c/Users/jasas/Work/OpenSource/pyTelops && git ls-remote --tags origin
```

Expected: `refs/tags/v0.2.0` shows. No PyPI upload — pyTelops stays private.

---

## Phase C: Lorenzo handoff note

### Task 18: Write the private MD note for Lorenzo

**Files:**
- Create: `C:\Users\jasas\Work\OpenSource\pyGigEVision_lorenzo_note.md` (lives outside any tracked repo — never committed)

- [ ] **Step 1: Create the note**

Create `C:\Users\jasas\Work\OpenSource\pyGigEVision_lorenzo_note.md`:

````markdown
# pyGigEVision — quickstart for open-flir

Private note — do not redistribute.

## What this package is

`pyGigEVision` is the pure-Python GigE Vision protocol layer (GVCP +
GVSP + GigE Vision standard register constants + GenICam XML download
helper). It is the shared foundation for any vendor-specific driver.
Everything in this package is generic — there are no Telops or FLIR
specifics anywhere.

It lives at https://github.com/ladisk/pyGigEVision (private). You're
already a maintainer.

## Install

From your open-flir venv:

```bash
pip install git+ssh://git@github.com/ladisk/pyGigEVision.git@v0.1.0
```

(SSH access uses your existing GitHub keys. If you prefer HTTPS, swap in
a personal access token: `git+https://<TOKEN>@github.com/...`.)

## Mapping from your current code

Anywhere you currently copy or vendorize from pyTelops, import from
pyGigEVision instead:

| Was (pyTelops copy) | Now (pyGigEVision import) |
|---|---|
| `pyTelops.gvcp.GVCPClient` | `from pyGigEVision import GVCPClient` |
| `pyTelops.gvcp.GVCPError` | `from pyGigEVision import GVCPError` |
| `pyTelops.gvsp.GVSPReceiver` | `from pyGigEVision import GVSPReceiver` |
| `REG_CCP`, `REG_HEARTBEAT_TIMEOUT`, `REG_FIRST_URL` | `from pyGigEVision.standard import REG_CCP, ...` |
| `REG_SC_HOST_PORT`, `REG_SC_PACKET_SIZE`, `REG_SC_PACKET_DELAY`, `REG_SC_DEST_ADDR`, `SC_PACKET_SIZE_MASK`, `SC_SCPS_DO_NOT_FRAGMENT`, `SC_SCPS_FIRE_TEST_PACKET` | `from pyGigEVision.standard import ...` |
| Inline GenICam XML download (the snippet from the FLIR guide step 2) | `from pyGigEVision import fetch_genicam_xml; xml, name = fetch_genicam_xml(client)` |

There is also an optional convenience `bootstrap()`:

```python
from pyGigEVision import bootstrap
client, xml = bootstrap("169.254.1.10")
# client is connected, has CCP, heartbeat ticking, and you have the XML
```

## What stays in your repo

Everything FLIR-specific. In particular:

- The vendor register map you derive from FLIR's GenICam XML
  (different addresses than Telops for Width / Height / ExposureTime,
  and a completely different vendor-feature set).
- Pixel byte-order: instantiate `GVSPReceiver(byte_order=">")` for FLIR
  (Telops uses `"<"`).
- Discovery field offsets if FLIR uses the standard GVCP discovery
  layout (Telops uses an extended layout with +24 byte offsets).
- Image header treatment: FLIR has no embedded header rows, unlike
  Telops which embeds 2 metadata rows per frame.
- No 16 GB onboard buffer logic — that's Telops-only.

## Minimal example: connect, configure, grab one frame

```python
from pyGigEVision import bootstrap, GVSPReceiver
from pyGigEVision.standard import (
    REG_SC_HOST_PORT, REG_SC_PACKET_SIZE, REG_SC_DEST_ADDR,
    SC_PACKET_SIZE_MASK,
)

CAMERA_IP = "169.254.1.10"
LOCAL_IP  = "169.254.1.1"

# 1. Boot: open GVCP, take control, start heartbeat, fetch XML
client, xml = bootstrap(CAMERA_IP)

# 2. (At this point you'd parse `xml` to discover Width/Height/PixelFormat
#    register addresses for THIS specific camera. Cache them in your
#    open-flir register map.)
REG_WIDTH  = 0x...   # from FLIR's XML
REG_HEIGHT = 0x...
REG_PIXEL_FORMAT = 0x...
REG_ACQUISITION_START = 0x...

# 3. Configure stream channel: where the camera should send packets
import socket, struct
local_ip_int = struct.unpack(">I", socket.inet_aton(LOCAL_IP))[0]

rx = GVSPReceiver(local_ip=LOCAL_IP, port=0, byte_order=">")  # FLIR is BE
rx.start()

client.write_reg(REG_SC_DEST_ADDR, local_ip_int)
client.write_reg(REG_SC_HOST_PORT, rx.port)

# Set jumbo packet size if your NIC supports it (8192). Otherwise 1500.
pkt_reg = client.read_reg(REG_SC_PACKET_SIZE)
flags = pkt_reg & ~SC_PACKET_SIZE_MASK
client.write_reg(REG_SC_PACKET_SIZE, flags | (1500 & SC_PACKET_SIZE_MASK))

# 4. Start acquisition (write to the FLIR-specific command register)
client.write_reg(REG_ACQUISITION_START, 1)

# 5. Grab one frame
frame = rx.read_frame(timeout=2.0)
print(frame.shape, frame.dtype)

# 6. Cleanup
rx.stop()
client.close()
```

That's it — pyGigEVision handles the protocol; your open-flir code
handles "what the registers mean" and "what to do with the pixels."

## Feedback loop

If pyGigEVision's API feels wrong or missing as you build open-flir,
open an issue on https://github.com/ladisk/pyGigEVision/issues. Every
piece of feedback is evidence about what shape (if any) a future
`BaseCamera` should take — Phase 2 is intentionally undecided until
both pyTelops and open-flir have shipped against the protocol layer.
````

- [ ] **Step 2: Verify the file is outside any tracked repo**

```bash
cd /c/Users/jasas/Work/OpenSource && git status pyGigEVision_lorenzo_note.md 2>&1 | head -3
```

Expected: `fatal: not a git repository` or `pathspec ... did not match any files`. The file lives in the OpenSource workspace folder, not inside any repo. **Do not** move it into pyGigEVision/ or pyTelops/.

- [ ] **Step 3: Hand-deliver to Lorenzo**

Send the file privately (Slack DM, email, signal — whatever you normally use) when you're ready. Not part of any automated pipeline.

---

## Self-review checklist (post-write)

Done by the plan author after writing the plan, not by the executing agent:

- ✅ **Spec coverage:** Every section of the spec has a task. Phase A = Tasks 1–9, Phase B = Tasks 10–17, Phase C = Task 18, validation gate = Task 16.
- ✅ **Placeholder scan:** No "TBD", "TODO", "implement later." All commands and code blocks are concrete.
- ✅ **Type/name consistency:** `bootstrap()` is defined in Task 6 and re-exported in Task 7; `fetch_genicam_xml()` and `parse_first_url()` likewise. `discover` is conditionally re-exported in Task 7 step 1 with the fallback noted. `pyGigEVision.standard` exports match what's imported in Tasks 11 and 18.
- ✅ **Order safety:** Imports in pyTelops are switched (Tasks 11–12) **before** the source files are deleted (Task 14), so nothing breaks intra-task. Tests are deleted/moved (Task 15) before the validation gate (Task 16).
- ✅ **Reversibility:** Every task ends in a commit. If anything goes sideways, `git revert` rolls back one task at a time.
