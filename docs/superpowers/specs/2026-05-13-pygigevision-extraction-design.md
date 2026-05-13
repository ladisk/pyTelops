# pyGigEVision Phase 1 extraction & pyTelops migration

**Date:** 2026-05-13
**Status:** Design approved, ready for implementation plan
**Repos affected:** `ladisk/pyGigEVision` (build), `ladisk/pyTelops` (migrate)
**Out of scope but supported:** Lorenzo's open-flir (private MD note hand-delivered separately)

---

## Goal

Lift the generic GigE Vision protocol code (GVCP, GVSP, standard registers,
GenICam XML download) out of pyTelops into a standalone package
`pyGigEVision`, then refactor pyTelops to depend on it. After Phase 1, both
pyTelops and Lorenzo's forthcoming open-flir can import the same protocol
primitives instead of each maintaining their own copies.

## Non-goals (Phase 1)

- No `BaseCamera` class. No framework hooks. Vendor drivers compose
  protocol primitives themselves.
- No CLI in pyGigEVision.
- No vendor-specific code, examples, or templates in the pyGigEVision repo.
  README, code, and tests stay vendor-agnostic. Vendor names appear nowhere
  in the public package.
- No GenICam XML *parsing* — only download. Each vendor's XML is parsed in
  the vendor driver.
- No PyPI release of pyGigEVision 0.1.0. Both repos stay private until the
  Telops collaboration discussions resolve and a coordinated public wave is
  scheduled.

## Architecture

Three repos, clear ownership:

```
ladisk/pyGigEVision  (private)        ladisk/pyTelops  (private)         (Lorenzo's open-flir)
├── src/pyGigEVision/                  ├── pyTelops/                     ├── (his structure)
│   ├── __init__.py                    │   ├── __init__.py               │
│   ├── gvcp.py        ← lifted        │   ├── camera.py    ← uses pyGV  │   uses pyGigEVision
│   ├── gvsp.py        ← lifted        │   ├── registers.py ← Telops     │
│   ├── standard.py    ← std regs      │   ├── cli.py                    │
│   ├── genicam.py     ← XML helper    │   ├── gui.py                    │
│   └── bootstrap.py   ← thin helper   │   └── (no gvcp/gvsp.py)         │
├── tests/                             ├── tests/                        │
└── pyproject.toml                     └── pyproject.toml                │
                                          dep: pyGigEVision>=0.1.0,<0.2.0
```

**Splitting principle.** If a thing only exists because Telops cameras are
weird, it stays in pyTelops. If it would work on a Basler/FLIR/Allied
Vision/Micro-Epsilon camera unchanged, it goes in pyGigEVision.

### What goes in each pyGigEVision module

| Module | Source | Contents |
|---|---|---|
| `gvcp.py` | lifted from `pyTelops/gvcp.py` | `GVCPClient`, `GVCPError`, status codes |
| `gvsp.py` | lifted from `pyTelops/gvsp.py` | `GVSPReceiver`, packet reassembly, payload auto-detect, byte-order parameter |
| `standard.py` | extracted from top of `pyTelops/gvcp.py` and `pyTelops/registers.py` | All GigE Vision **standard** registers in one place: bootstrap (`REG_CCP`, `REG_HEARTBEAT_TIMEOUT`, `REG_FIRST_URL`) + Stream Channel (`REG_SC_HOST_PORT`, `REG_SC_PACKET_SIZE`, `REG_SC_PACKET_DELAY`, `REG_SC_DEST_ADDR`, packet-size flag bits/mask). `gvcp.py` imports from here for its own internal use. |
| `genicam.py` | new (logic exists today inlined in pyTelops + the FLIR guide) | `fetch_genicam_xml(client) -> (xml_bytes, filename)` — read URL @ `0x0200`, download, decompress if zipped |
| `bootstrap.py` | new (~30 lines) | `bootstrap(camera_ip) -> (GVCPClient, xml_bytes)` — opens GVCP, sets CCP, starts heartbeat, fetches XML. Optional convenience for vendor drivers. |

### What stays in pyTelops

- All of `camera.py` (vendor logic — cooldown wait, calibration loading,
  `.tsco` parsing, 2-row header strip, RT-mode Celsius conversion, 16 GB
  onboard buffer record/download, `packet_delay` property, `latest=True`
  drain, all property accessors)
- Vendor `registers.py` (Telops-specific addresses + enums:
  `CalibrationMode`, `ExposureAuto`, `MemoryBufferMOISource`, etc.)
- `cli.py`, `gui.py`
- The `TELOPS_MANUFACTURER` discovery filter — pyGigEVision's `discover()`
  returns all vendors, pyTelops's `discover()` wraps it and filters

## pyGigEVision API surface (Phase 1)

```python
# Discovery
from pyGigEVision import discover
cameras = discover(timeout=2.0)
# -> [{ip, manufacturer, model, serial, device_version, user_name}, ...]

# Low-level control
from pyGigEVision import GVCPClient, GVCPError
with GVCPClient("169.254.67.34") as ctrl:
    width = ctrl.read_reg(0xD300)
    ctrl.write_reg(0xE810, value=2000)
    exposure = ctrl.read_float(0xE808)
    ctrl.write_float(0xE808, 100.0)
    raw_xml = ctrl.read_mem(addr, size)

# Streaming
from pyGigEVision import GVSPReceiver
rx = GVSPReceiver(local_ip="169.254.67.1", port=0, byte_order="<")
rx.start()
frame = rx.read_frame(timeout=1.0)                 # ordered
frame = rx.read_frame(timeout=1.0, latest=True)    # drain to newest
rx.stop()

# GenICam XML
from pyGigEVision import fetch_genicam_xml
xml_bytes, filename = fetch_genicam_xml(gvcp_client)

# Optional bootstrap helper
from pyGigEVision import bootstrap
ctrl, xml_bytes = bootstrap(camera_ip)

# Standard register constants
from pyGigEVision.standard import (
    REG_CCP, REG_HEARTBEAT_TIMEOUT, REG_FIRST_URL,
    REG_SC_HOST_PORT, REG_SC_PACKET_SIZE, REG_SC_PACKET_DELAY, REG_SC_DEST_ADDR,
    SC_PACKET_SIZE_MASK, SC_SCPS_DO_NOT_FRAGMENT, SC_SCPS_FIRE_TEST_PACKET,
)

# Errors
from pyGigEVision import GVCPError
```

### Conventions

- Package import name: `pyGigEVision`. PyPI normalizes to `pygigevision`.
- Logging: `logging.getLogger("pyGigEVision.gvcp")`, `pyGigEVision.gvsp`
  (renamed during the lift; pyTelops currently uses `pyTelops.camera`).
- Byte order is a `GVSPReceiver` constructor parameter (already is —
  `"<"` for Telops, `">"` for FLIR).
- `discover()` returns every GigE Vision device on the network. Vendor
  filtering is the caller's responsibility.
- `genicam.fetch_genicam_xml` returns raw XML bytes; parsing is per-vendor.
- Dependencies: `numpy` only. `tqdm` is dropped from pyGigEVision (it was
  only used in pyTelops's buffer-download progress, which stays in
  pyTelops).
- Python `>= 3.10` (matches pyTelops baseline).

## pyTelops migration

Mechanical refactor. No behavior changes for pyTelops users.

1. Add `pyGigEVision @ git+ssh://git@github.com/ladisk/pyGigEVision.git@v0.1.0`
   to `pyproject.toml` dependencies. Pin range: `>= 0.1.0, < 0.2.0`.
2. Delete `pyTelops/gvcp.py` and `pyTelops/gvsp.py`.
3. Trim `pyTelops/registers.py`: remove the standard SC and bootstrap
   register constants at the top. Keep all Telops-specific addresses and
   all enums.
4. Update imports in `camera.py`:
   ```python
   from pyGigEVision import GVCPClient, GVCPError, GVSPReceiver
   from pyGigEVision.standard import REG_HEARTBEAT_TIMEOUT
   ```
5. Update `pyTelops/__init__.py` to re-export `GVCPClient` and `GVCPError`
   from pyGigEVision (preserves back-compat for any user importing them
   from pyTelops). All other exports unchanged.
6. Update `cli.py`, `gui.py` for any direct gvcp/gvsp references.
7. Move `tests/test_gvcp.py` and `tests/test_gvsp.py` to pyGigEVision;
   update import paths there. Other pyTelops tests update their imports
   in place.
8. Bump pyTelops `0.1.0` → `0.2.0`. Tag `v0.2.0` on private repo. No PyPI
   push.

### What does not change

- pyTelops's high-level `Camera` API (`cam.integration_time`, `cam.grab()`,
  `cam.buffer_record()`, etc.) is byte-identical for users.
- `discover()` still defaults to filtering Telops manufacturer; that filter
  logic lives in pyTelops.
- All vendor logic in `camera.py` — cooldown wait, calibration loading, RT
  mode Celsius, 16 GB buffer, packet_delay, latest=True drain — unchanged.
- All Telops-specific registers and enums stay in `pyTelops/registers.py`.

## Sequencing

```
Step 1: Build pyGigEVision Phase 1
        ├── Lift gvcp.py from pyTelops (verbatim, rename logger only)
        ├── Lift gvsp.py from pyTelops (verbatim, rename logger only)
        ├── Create standard.py (extract std regs)
        ├── Create genicam.py (XML download/parse helper)
        ├── Create bootstrap.py (~30 line convenience)
        ├── Update __init__.py exports
        ├── Lift test_gvcp.py + test_gvsp.py (port to pyGigEVision)
        ├── Add test_standard.py + test_genicam.py
        ├── Commit + push to ladisk/pyGigEVision main
        ├── Bump 0.0.1 → 0.1.0, tag v0.1.0, push tag
        └── pip install -e into pyTelops's venv

  ↓ GATE: pyGigEVision tests green, importable from pyTelops's venv

Step 2: Migrate pyTelops onto pyGigEVision
        ├── Add pyGigEVision dep to pyproject.toml (git URL pin)
        ├── Delete pyTelops/gvcp.py, gvsp.py
        ├── Trim pyTelops/registers.py
        ├── Update imports in camera.py, __init__.py, cli.py, gui.py
        ├── Update remaining test imports
        └── Run full pyTelops test suite (161 unit + 57 hardware)

  ↓ GATE: full pyTelops suite green, no behavior changes

Step 3: Tag pyTelops 0.2.0
        └── Tag, push to private repo. No PyPI push.

Step 4: Lorenzo deliverable (independent, anytime after Step 1)
        └── Hand-deliver private MD note (see "Lorenzo note" section)
```

### Why this ordering matters

- pyGigEVision must exist and be tested in isolation first — otherwise
  pyTelops is being refactored against a moving target.
- Tests get lifted *with* the code in Step 1. pyGigEVision starts with
  proven test coverage on day one.
- pyTelops migration is a near-mechanical refactor; most failure modes are
  import-path typos that the test suite catches immediately.
- Step 4 is independent of Steps 2–3. Lorenzo can move whenever he wants.

## Testing strategy

**pyGigEVision:**
- `test_gvcp.py` — lifted verbatim, imports updated.
- `test_gvsp.py` — lifted verbatim, imports updated.
- `test_standard.py` — new, ~10 lines sanity-checking the standard
  register constants have correct values.
- `test_genicam.py` — new, ~50 lines, mocks GVCP and verifies XML
  download + unzip path. No hardware needed.
- CI: GitHub Actions, Python 3.10–3.13, Windows + Linux (mirror
  pyTelops's matrix).
- No hardware tests in pyGigEVision — pyGigEVision is vendor-agnostic;
  hardware coverage stays in pyTelops where there's a real Telops camera.

**pyTelops after migration:**
- Test counts unchanged (test_gvcp.py and test_gvsp.py removed because
  they moved; their coverage is preserved in pyGigEVision).
- All other tests run, only imports change.
- Validation gate: full suite (161 unit + 57 hardware) must pass before
  tagging 0.2.0.

**Risk mitigation:**
- Lift gvcp.py and gvsp.py *verbatim*. No refactoring during the lift —
  only logger name changes. Keeps the diff small and the failure modes
  obvious.
- Run pyGigEVision tests in pyTelops's venv with editable install before
  any pyTelops migration commits. Catches import-surface issues early.

## Logistics

**pyGigEVision:**
- Develop directly on `main` of `ladisk/pyGigEVision`. Local dir is
  already a git repo on main, clean, tracking origin.
- Initial real commit replaces the placeholder `__init__.py` with the
  lifted modules.
- After Phase 1 lands and tests pass: bump `pyproject.toml` from `0.0.1`
  to `0.1.0`, tag `v0.1.0`, push tag.
- **No PyPI upload.** The `0.0.1` placeholder stays on PyPI for name
  reservation. `0.1.0` lives only in the private GitHub repo until the
  coordinated public wave (out of scope for this plan).
- README.rst gets a real description (still vendor-agnostic).
  PYPI_README.rst stays as the placeholder until public release.
- Disclaimer about "GigE Vision" trademark stays in README per
  legal-analysis memory.

**pyTelops:**
- Stays private on `ladisk/pyTelops`.
- Bumps `0.1.0` → `0.2.0` after migration tests green.
- No PyPI push (still pre-collaboration, not public).

**Dependency pin in pyTelops:**
- `pyGigEVision @ git+ssh://git@github.com/ladisk/pyGigEVision.git@v0.1.0`
  during the private-repo phase.
- Pin range `>= 0.1.0, < 0.2.0` while we're in 0.x and the API may shift.
- Loosen to `>= 0.1.0` once the surface stabilizes.

## Lorenzo note (delivered privately, outside any tracked repo)

Single Markdown file at `C:\Users\jasas\Work\OpenSource\pyGigEVision_lorenzo_note.md`.
Hand-delivered after Step 1 lands; never committed anywhere.

Outline:

1. **What pyGigEVision is** — pure-Python GigE Vision protocol layer
   (GVCP + GVSP + standard regs + GenICam XML download). The shared base
   that both Telops and FLIR drivers run on.
2. **How to install** — `pip install git+ssh://git@github.com/ladisk/pyGigEVision.git@v0.1.0`
   from his open-flir venv. He's already a maintainer on the private repo.
3. **Mapping from his current code** — for each thing he's currently
   copying from `pyTelops/`, what to import from `pyGigEVision` instead:
   - `pyTelops.gvcp.GVCPClient` → `pyGigEVision.GVCPClient`
   - `pyTelops.gvsp.GVSPReceiver` → `pyGigEVision.GVSPReceiver`
   - `pyTelops.registers.REG_SC_*` and bootstrap regs →
     `pyGigEVision.standard.REG_SC_*`
   - GenICam XML download (currently inlined per the FLIR guide step 2)
     → `pyGigEVision.fetch_genicam_xml(client)`
4. **FLIR-specific things he keeps in his repo** — vendor register map
   (from FLIR's GenICam XML), pixel byte-order
   (`GVSPReceiver(byte_order=">")`), discovery field offsets if FLIR uses
   standard, image header treatment (no header rows to strip on FLIR), no
   16 GB onboard buffer logic.
5. **Minimal example (~50 lines)** — connect → set CCP → start heartbeat
   → fetch XML → set up GVSP → grab a frame, using only `pyGigEVision`
   primitives. Shows the lifecycle pattern without dictating it.
6. **Open questions to feed back** — when he hits something where
   pyGigEVision's API feels wrong or missing, he opens an issue on
   `ladisk/pyGigEVision`. His feedback is what eventually drives whether
   `BaseCamera` is worth extracting in a future Phase 2.

## Out of scope / deferred

- `BaseCamera` class — wait until open-flir lands and we see whether the
  lifecycle pattern actually repeats. Standard "extract abstraction at
  three implementations, not one" rule.
- CLI for pyGigEVision — trivial to add later; nobody needs it now.
- Examples or vendor template in the pyGigEVision repo — clarified
  earlier; vendor template can be added later when a clean, generic shape
  is obvious.
- GenICam XML *parsing* — only download is in scope.
- Auto-discovery of FLIR-style discovery offsets — Lorenzo handles in his
  vendor driver.
- LDAQ integration changes — orthogonal, untouched.
- Public PyPI release of pyGigEVision 0.1.0 — coordinated wave with
  pyTelops going public, scheduled separately.

## References

- pyTelops current source: `C:\Users\jasas\Work\OpenSource\pyTelops\`
- pyGigEVision current state: `C:\Users\jasas\Work\OpenSource\pyGigEVision\`
  (placeholder 0.0.1, on `main`, clean)
- Memory: `pytelops-package.md`, `pygigevision-package.md`,
  `py-gigevision-opportunity.md`, `collaborator_lollocappo.md`,
  `telops-collaboration.md`, `legal-analysis.md`
- FLIR driver guide:
  `C:\Users\jasas\Work\OpenSource\OpenEOL\workspace\notes\flir-driver-guide.md`
