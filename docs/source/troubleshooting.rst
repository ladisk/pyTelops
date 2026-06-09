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

- ``discover()`` now searches every host network interface by default,
  so cameras on USB-to-GigE adapters and secondary NICs are found
  without selecting an interface by hand. Each result dict carries
  ``reachable`` (whether the camera is on a subnet served by one of your
  host NICs) and ``interface_ip`` (the host interface the camera replied
  on). If discovery still returns nothing, work through the fixes below.
- Confirm the camera has a link-local address (``169.254.x.x``) and
  your host has a matching link-local IP on the same Ethernet adapter.
  ``ipconfig`` on Windows, ``ip a`` on Linux.
- Run ``pytelops setup`` as administrator once per environment; it
  prints the Windows firewall rule you need to allow inbound UDP for
  ``python.exe``. Python environment changes (new venv, Anaconda vs
  system Python) require a new rule per binary.
- On Linux, raise the kernel UDP receive buffer:
  ``sudo sysctl -w net.core.rmem_max=16777216``.
- Rule out corporate endpoint security per-binary WFP filters. These
  can silently drop UDP for a specific ``python.exe`` even with Windows
  Firewall disabled. Test by running the same code with a different
  Python binary.

VPN virtual adapter shadows the camera adapter
----------------------------------------------

If discovery finds no camera (or only unrelated GigE Vision devices) while
a VPN is running, its virtual link-local adapter is shadowing the Ethernet
adapter that the camera is on. Stop the VPN service and disable its virtual
network adapter from the Windows network settings, then retry discovery.
Re-enable the VPN once you are done.

Camera found but not reachable (wrong subnet)
---------------------------------------------

**Symptom:** ``discover()`` lists the camera, but its result dict has
``reachable == False``, and ``Camera().connect()`` raises an actionable
error explaining that the camera is on no host NIC subnet.

**Cause:** the camera came up with an IP address that does not match any
subnet served by your host network interfaces (for example, a static
address from a previous network, or a leftover configuration). There is
no local interface from which the host can talk to it.

**Fix:** re-home the camera onto a reachable subnet with
:func:`~pyTelops.force_ip`, which assigns a new address by MAC, then
re-discover:

.. code-block:: python

   from pyTelops import discover, force_ip

   camera = next(c for c in discover() if not c["reachable"])
   force_ip(camera, "169.254.10.50", "255.255.0.0")
   # The camera reboots its IP stack; wait a moment, then re-discover.

After ``force_ip`` returns, allow about a second for the camera to apply
the new address before calling ``discover()`` again. Choose an address on
one of your host NIC subnets so the re-discovered result is reachable.

``ACCESS_DENIED`` on reconnect
------------------------------

**Symptom:** After a crashed kernel or ``Ctrl-C``, ``cam.connect()``
prints ``ACCESS_DENIED: waiting for stale CCP lock to expire...`` and
hangs for several seconds.

**Cause:** the previous session held GVCP control (CCP) and didn't
release it cleanly. The camera keeps the old session alive until its
heartbeat timeout expires.

**Fix:** this is normal. Wait up to ~15 seconds -- pyTelops polls the
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
end-to-end latency bounded to one frame period plus one render. Use
``latest=False`` (the default) for measurement or logging where you
need every frame in order.

Buffer download fails or is unreliable
--------------------------------------

**Symptom:** ``cam.buffer_download()`` raises ``FrameIntegrityError``, or
returns data that completed with missing frames.

**Cause:** the download saturates GigE at ~45 MB/s and has effectively
no headroom. Any competing network or CPU load, most commonly a video
call or other heavy real-time network and CPU load, can starve the
receiver enough to drop frames. The camera has no backpressure: it
pushes at the configured bitrate regardless of whether the host is
keeping up.

**What ``buffer_download`` does about it:** the download verifies frame
integrity and self-recovers. It re-streams any frame that arrived
incomplete or never arrived, at a paced lower bitrate, until the
transfer is complete. By default (``max_dropped_frames=0``) it raises
``FrameIntegrityError`` if any frame is still incomplete after recovery;
pass ``max_dropped_frames=N`` to tolerate up to ``N`` incomplete frames
and get the array back. Every call attaches ``cam.last_download_stats``
(a ``DownloadStats`` with ``n_incomplete``, ``throughput_mbps``,
``packet_size_used``, ``bitrate_used``, and more), so you can check
download quality without inspecting pixels.

**Fixes:**

- Reduce competing load: close a video call or other heavy real-time
  network and CPU load entirely (not just minimize it) before the
  download.
- Find a stable configuration with :func:`~pyTelops.tune_connection`,
  which probes the link and sweeps download settings, then store it with
  ``report.apply(cam)`` for subsequent downloads.
- Raise ``cam.packet_delay`` to pace the download. Inserting gaps
  between packets gives a host or adapter that cannot keep up at full
  rate time to drain its receive queue, at some cost to peak throughput.
- Lower the download bitrate to leave headroom:

  .. code-block:: python

     data = cam.buffer_download(sequence=0, bitrate_mbps=500)

  At 500 Mbps the download takes longer but is much more robust to
  competing load.

- Ensure the camera adapter is on a dedicated Ethernet port, not
  shared with the internet (Wi-Fi for internet, Ethernet for camera).

Firewall rule reset after power cycle
--------------------------------------

**Symptom:** The camera worked before, but after a power cycle or
reboot, ``discover()`` returns nothing or streaming produces only
``packets unrecoverable`` errors.

**Cause:** on some Windows configurations the per-program firewall rule
for ``python.exe`` is reset or removed when the system restarts or
when a Windows Update runs.

**Fix:** re-run ``pytelops setup`` as administrator to restore the rule.
You can verify the rule exists with:

.. code-block:: powershell

   netsh advfirewall firewall show rule name="pyTelops-GVSP"

If the output is empty, the rule was removed. Re-add it manually:

.. code-block:: powershell

   netsh advfirewall firewall add rule name="pyTelops-GVSP" dir=in `
       action=allow protocol=UDP `
       program="C:\path\to\python.exe"

Replace the path with the full path to the Python binary in your active
environment.

Resolution cycling crash
------------------------

**Symptom:** After changing ``cam.resolution`` several times in quick
succession (for example, in a loop or during exploratory use in a
notebook), the camera stops responding and has to be power-cycled.

**Cause:** the camera firmware does not tolerate rapid resolution changes.
Each resolution change triggers a sensor reset sequence; issuing a new
change before the previous reset completes can leave the firmware in an
inconsistent state.

**Fix:** allow at least 1 second between resolution changes. If the
camera is unresponsive, power-cycle it -- the firmware state is reset on
boot and the camera will be ready again after its cooldown sequence
completes (typically 2--3 minutes from a cold start).
