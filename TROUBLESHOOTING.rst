Troubleshooting
===============

Symptoms, causes, and fixes for the most common issues you may
encounter while using pyTelops.

Camera not found on ``discover()`` / ``connect()``
--------------------------------------------------

**Symptom:** ``discover()`` returns an empty list, or ``cam.connect()``
raises ``RuntimeError: No Telops camera found``.

**Cause:** firewall, wrong network adapter, or the camera is
unreachable.

**Fixes:**

- Confirm the camera has a link-local address (``169.254.x.x``) and
  your host has a matching link-local IP on the same Ethernet adapter.
  ``ipconfig`` on Windows, ``ip a`` on Linux.
- Run ``pytelops setup`` as administrator once per environment — it
  prints the Windows firewall rule you need to allow inbound UDP for
  ``python.exe``. Python environment changes (new venv, Anaconda vs
  system Python) require a new rule per binary.
- On Linux, raise the kernel UDP receive buffer:
  ``sudo sysctl -w net.core.rmem_max=16777216``.
- Rule out Tailscale or corporate endpoint security per-binary WFP
  filters. These can silently drop UDP for a specific ``python.exe``
  even with Windows Firewall disabled. Test by running the same code
  with a different Python binary.

``ACCESS_DENIED`` on reconnect
------------------------------

**Symptom:** After a crashed kernel or ``Ctrl-C``, ``cam.connect()``
prints ``ACCESS_DENIED: waiting for stale CCP lock to expire...`` and
hangs for several seconds.

**Cause:** the previous session held GVCP control (CCP) and didn't
release it cleanly. The camera keeps the old session alive until its
heartbeat timeout expires.

**Fix:** this is normal. Wait up to ~15 seconds — pyTelops polls the
camera and auto-recovers once the heartbeat timeout elapses. If it
takes longer, power-cycle the camera.

Live streaming: ``packets unrecoverable`` warnings
--------------------------------------------------

**Symptom:** Live streaming at higher frame rates logs
``Frame N: X/113 packets unrecoverable`` warnings, and frames drop out
or show diagonal artifacts.

**Cause:** at the camera's default packet delay of 0, all ~113 packets
of a frame are sent back-to-back at GigE line rate, a ~1.4 ms burst.
Any host-side hiccup (garbage collection, matplotlib redraw, Qt event
loop jitter) can overflow the kernel UDP receive queue during the
burst.

**Fix:** spread the packets in time via ``cam.packet_delay``:

.. code-block:: python

   cam.packet_delay = 1000   # ~8 us between packets (2 ms per frame)
                             # safe up to ~400 fps

Start with ``1000``; bump to ``2000`` or ``5000`` if you still see
losses under heavy host load. No effect on buffer recording or buffer
download, which use separate mechanisms.

Live display lag that grows over time
-------------------------------------

**Symptom:** A live processing loop starts in sync with the scene but
the displayed image drifts further behind real time every second.
Waving your hand and freezing shows a constant delay on the first few
seconds, then an increasingly larger delay after a minute.

**Cause:** the consumer (matplotlib redraws, Qt event loop, scipy
processing, etc.) can't keep up with the camera frame rate. Frames
accumulate in pyTelops's internal GVSP queue and the loop processes
them in order, each one staler than the last.

**Fix:** use ``latest=True`` in the display loop to drain the queue
and always show the most recent frame:

.. code-block:: python

   with cam.acquisition():
       while running:
           frame = cam.read_frame(timeout=0.1, latest=True)
           if frame is not None:
               process_and_display(frame)

This drops intermediate frames during slow periods but keeps
end-to-end latency bounded to one frame period + one render. Use
``latest=False`` (the default) for measurement or logging where you
need every frame in order.

Buffer download fails or is unreliable
--------------------------------------

**Symptom:** ``cam.buffer_download()`` stalls partway through, completes
with missing frames, or fails the integrity check.

**Cause:** the download saturates GigE at ~45 MB/s and has effectively
no headroom. Any competing network or CPU load — most commonly an
active **Microsoft Teams / Zoom / Google Meet call**, or heavy
background processes — can starve the receiver enough to drop frames.
The camera has no backpressure: it pushes at the configured bitrate
regardless of whether the host is keeping up.

**Fixes:**

- **Best:** close real-time communication apps entirely (not just
  minimize them) before the download.
- Lower the download bitrate to leave headroom:

  .. code-block:: python

     data = cam.buffer_download(sequence=0, bitrate_mbps=500)

  At 500 Mbps the download takes twice as long but is much more
  robust to competing load.

- Ensure the camera adapter is on a dedicated Ethernet port, not
  shared with the internet (Wi-Fi for internet, Ethernet for camera).
