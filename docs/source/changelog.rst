Changelog
=========

Unreleased
----------

- ``discover()`` now finds cameras on every host network interface (USB-to-GigE
  adapters, secondary NICs), via the reworked multi-interface discovery in
  pyGigEVision. Each result carries a ``reachable`` flag and an ``interface_ip``
  recording the host NIC the camera replied on.
- ``Camera()`` now connects through the interface the camera replied on during
  discovery, so a host with multiple link-local NICs no longer needs manual
  interface selection. It also raises an actionable error when the selected
  camera is on no host NIC subnet, instead of failing later with a confusing
  OS error.
- Added ``pyTelops.force_ip(camera, ip, mask, gateway=None)`` to re-home a
  wrong-subnet camera by MAC (GVCP FORCEIP).
- Removed the host-side link-local probe that the multi-interface discovery
  makes redundant. Thanks to Lorenzo Capponi (LolloCappo) for the
  connected-socket interface-detection approach (PR #13) that informed this work.
- Requires the updated pyGigEVision (multi-interface discovery, ``force_ip``).
- ``buffer_download`` now detects dropped and corrupted frames and raises
  ``FrameIntegrityError`` by default when any frame is incomplete. This is a
  behavior change: pass ``max_dropped_frames=N`` to tolerate up to ``N``
  incomplete frames, as older code relied on the method always returning an
  array.
- ``buffer_download`` attaches a ``DownloadStats`` integrity report to
  ``cam.last_download_stats`` (per-frame missing packets, resend counts,
  throughput) so callers can inspect data quality without pixel inspection.
- ``buffer_download`` enables GVSP packet resends during the stream and
  re-downloads incomplete frames from the camera buffer, controlled by the new
  ``resend`` and ``retries`` parameters. It no longer suppresses the
  ``pyGigEVision.gvsp`` packet-loss warnings.
- Corrected the misleading ``packet_size=9000`` guidance. Oversized requests on
  a non-jumbo path are now detected with a FireTestPacket path probe, and the
  download warns and falls back to ``packet_size=1500`` instead of silently
  emitting mostly-zero frames.
- Added ``tune_connection()`` to probe the link and sweep download settings,
  recommending a stable and fast configuration for the current adapter and
  cable. Includes an opt-in read-only NIC diagnostics pass.
- New public names: ``FrameIntegrityError``, ``DownloadStats``,
  ``ConnectionReport``, ``tune_connection``.

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
