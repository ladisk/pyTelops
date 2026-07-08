Changelog
=========

Version 0.2.3 (unreleased)
--------------------------

- **Fixed silent frame decimation of buffer downloads when a non-zero**
  ``packet_delay`` **is set.** A live-streaming ``packet_delay`` (GVSP
  inter-packet delay / SCPD) was being applied to memory-buffer downloads too,
  where it makes the camera silently return **every other frame** (a 2x, or with
  heavy throttling higher, decimation): the throttled transmit desyncs from the
  camera's buffer-read clock, so it skips frames -- with contiguous block-ids and
  no error, so the data looked valid but was time-aliased (a signal at frequency
  ``f`` appeared at ``2f``). ``buffer_download`` now forces SCPD to 0 for the
  transfer and restores the user's value afterwards, so downloads are correct
  regardless of the live ``packet_delay``. For host-side packet loss during
  download use jumbo packets and/or a lower ``bitrate_mbps`` (both proven), NOT
  ``packet_delay``. Confirmed on hardware (FAST-M3k, firmware 3.1.13.2): with
  ``packet_delay=1000`` the same recording strided before the fix and downloads
  cleanly after.
- **Large buffer downloads are now streamed in chunks (default 1000 frames per
  session).** A single large acquisition session overruns the host receive path
  and the camera's paced readout: an 8000-frame session lost most of its frames
  (thousands incomplete at idle on a USB-GigE host) and returned them
  mis-ordered, whereas sessions of at most a thousand frames come back complete.
  ``buffer_download`` now splits the range into ``chunk_size`` pieces, so large
  downloads are **100% complete** at idle and under CPU load (validated on
  hardware). The recovery loop also no longer paces the bitrate *down* between
  rounds -- a low download bitrate is what makes the camera stride (skip frames),
  so lowering it to fight packet loss was counter-productive; it stays at the
  base rate. Use jumbo packets and/or a lower base ``bitrate_mbps`` for loss.
- ``buffer_download`` now verifies frame **ordering** using the per-frame camera
  leader timestamps and raises ``FrameIntegrityError`` when more than
  ``order_tolerance`` (default 5%) of the frames are mis-ordered, duplicated, or
  strided (new ``verify_order`` parameter, default ``True``). This backstop
  catches gross corruption such as the decimation above (which mis-orders ~half
  the frames) even though every packet arrives complete. A small (~1%)
  irreducible residual of cross-session mis-ordering remains on large downloads
  -- the camera does not halt a download session promptly, so a session's tail
  can leak into the next -- and is recorded and logged but tolerated rather than
  failing every download. The anomaly counts are on ``cam.last_download_stats``
  (``n_out_of_order``, ``n_stride_gaps``); the check is skipped when the camera
  does not populate timestamps. Pass ``order_tolerance=0`` for strict behaviour
  or ``verify_order=False`` to accept frames as delivered.
- **Fixed stale frames leaking between recovery rounds of a buffer download on a
  heavily loaded host.** Each paced recovery round re-streams the still-missing
  frames in a fresh GVSP session, and the session's block id (which restarts at 1)
  is used to place each frame at ``offset = block_id - 1``. Under host CPU
  saturation the GVSP receiver could fall behind and leave a frame from the
  previous session queued -- or unread in the OS socket buffer -- so the next
  session read it first and mapped it to the **wrong position** (packet-complete
  but out-of-order / strided frames, which ``verify_order`` above then reported).
  The receiver now exposes ``flush()`` and ``buffer_download`` drains the frame
  queue, partial frame buffers, and the socket receive buffer at each session
  boundary, so recovery is correct under load. Requires
  ``pyGigEVision>=0.2.2`` (``GVSPReceiver.flush``).

Version 0.2.2
-------------

- First release published to PyPI.
- The pyGigEVision dependency now installs from PyPI (``pyGigEVision>=0.2.1``)
  instead of a git URL; automated CI runs on push and pull request again.
- ``Camera(ip=...).connect()`` now runs a discovery sweep to bind the host
  interface that actually reaches the camera, instead of trusting OS routing.
  On hosts with several link-local interfaces (VPN, Bluetooth, virtual
  adapters) the route by metric could pick a dead interface and the connect
  timed out. Cameras the sweep cannot see fall back to OS routing as before.
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
- ``buffer_download`` now auto-tunes by default: it probes once per connection
  whether the path carries jumbo frames and learns a download bitrate from
  each transfer's completeness. Pass ``packet_size`` or ``bitrate_mbps`` to
  override, or set ``cam.auto_tune = False`` to disable.
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
