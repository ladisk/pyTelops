Changelog
=========

Unreleased
----------

- ``discover()`` now finds cameras on every host network interface (USB-to-GigE
  adapters, secondary NICs), via the reworked multi-interface discovery in
  pyGigEVision. Each result carries a ``reachable`` flag.
- ``Camera()`` now raises an actionable error when the selected camera is on no
  host NIC subnet, instead of failing later with a confusing OS error.
- Added ``pyTelops.force_ip(camera, ip, mask, gateway=None)`` to re-home a
  wrong-subnet camera by MAC (GVCP FORCEIP).
- Removed the host-side link-local probe that the multi-interface discovery
  makes redundant. Thanks to Lorenzo Capponi (LolloCappo) for the
  connected-socket interface-detection approach (PR #13) that informed this work.
- Requires the updated pyGigEVision (multi-interface discovery, ``force_ip``).

Version 0.2.1
-------------

- Adopt sdypy package template conventions: hatchling build, sphinx-book-theme
  docs on ReadTheDocs, manual changelog, version-sync release script.
- Add full Sphinx documentation: getting started, Camera API reference,
  calibration, streaming and buffer, troubleshooting.
- Add five runnable examples in ``examples/``: connect and grab, continuous
  live view, buffer recording, calibration loading, external trigger.
- Add ``CONTRIBUTING.rst`` and this changelog. Fold the standalone
  troubleshooting guide into the documentation.
- Switch lint from flake8 to ruff (strict superset, includes formatter).
- Polish: NumPy-style docstrings and complete type hints across the Camera
  class and the registers, CLI, and GUI modules.
- No public API changes; the LDAQ Telops plugin and existing user code
  continue to work unchanged.

Version 0.2.0
-------------

- Split the GigE Vision protocol layer into the standalone `pyGigEVision
  <https://github.com/ladisk/pyGigEVision>`_ package. pyTelops becomes the
  Telops vendor layer on top of it.
- ``Camera`` re-exports ``GVCPClient`` and ``GVCPError`` from pyGigEVision for
  back-compatibility.
- 127 unit tests plus 57 hardware tests.

Version 0.1.0
-------------

- Initial Telops camera driver: discovery, control, live streaming, onboard
  buffer recording and download, calibration loading, NUC, diagnostics.
