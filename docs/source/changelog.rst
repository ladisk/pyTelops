Changelog
=========

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
