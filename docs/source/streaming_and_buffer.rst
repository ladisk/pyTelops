Streaming and buffer recording
==============================

pyTelops supports two acquisition modes: live streaming (frames sent to the PC
in real time) and buffer recording (frames recorded to the camera's internal
16 GB memory at full sensor speed, then downloaded to the PC afterwards).
Choose the right mode for your measurement.

Live streaming
--------------

In streaming mode the camera sends each frame over Ethernet as it is captured.
GigE bandwidth limits throughput to roughly 125 MB/s, which supports up to
approximately 760 fps at full resolution (320x256).

Start and stop streaming with :meth:`pyTelops.Camera.acquisition_start` and
:meth:`pyTelops.Camera.acquisition_stop`, or use the context manager shorthand:

.. code-block:: python

    with cam.acquisition():
        while running:
            frame = cam.read_frame(timeout=0.1, latest=True)
            if frame is not None:
                process_and_display(frame)

Pass ``latest=True`` to :meth:`pyTelops.Camera.read_frame` in display loops to
always show the most recent frame rather than processing a growing backlog.  See
:doc:`troubleshooting` for details on the growing-lag symptom.

For short captures you can use the convenience methods directly:

.. code-block:: python

    frame  = cam.grab()          # single frame -> numpy (H, W)
    frames = cam.acquire(100)    # 100 consecutive frames -> numpy (N, H, W)

Packet delay tuning
~~~~~~~~~~~~~~~~~~~

At the camera's default ``packet_delay`` of 0, all packets of a frame are sent
back-to-back in a ~1.4 ms burst.  At higher frame rates this can overflow the
host UDP receive buffer.  If you see ``packets unrecoverable`` warnings, spread
the burst:

.. code-block:: python

    cam.packet_delay = 1000   # ~8 us between packets; safe up to ~400 fps

Start with ``1000`` and increase to ``2000`` or ``5000`` under heavy host load.
Packet delay does not affect buffer recording: the camera fills its internal
buffer at full speed regardless. It does pace buffer download, where raising it
inserts gaps between packets and can remove dropped frames on a host or adapter
that cannot keep up at full rate, at some cost to peak throughput. See the
buffer-download section below.

Buffer recording
----------------

The onboard 16 GB buffer lets the camera record at the full sensor speed
(up to 95k fps) independently of GigE bandwidth.  The workflow is:
configure the buffer, record, then download.

.. code-block:: python

    from pyTelops import Camera

    with Camera() as cam:
        cam.frame_rate = 2000.0
        cam.integration_time = 30.0

        # Allocate three sequences of 5 seconds each
        cam.buffer_configure(n_sequences=3, duration=5.0,
                             moi_source="software")

        # Record all sequences in one call
        cam.buffer_record()    # arms, fires MOI for each, waits, stops

        # Inspect what was recorded
        print(cam.buffer_info())
        # {'status': 'IDLE', 'n_sequences': 3, 'recorded': [10000, 10000, 10000], ...}

        # Download selected sequences
        data_0 = cam.buffer_download(sequence=0)
        data_2 = cam.buffer_download(sequence=2)

        cam.buffer_clear()

:meth:`pyTelops.Camera.buffer_record` prints per-sequence progress::

    Arming (seq 1/3)... Recording... Done (10000 frames)
    Firing (seq 2/3)... Recording... Done (10000 frames)
    Firing (seq 3/3)... Recording... Done (10000 frames)

:meth:`pyTelops.Camera.buffer_download` shows a tqdm progress bar and an
integrity check::

    Downloading: 100%|██████████| 10000/10000 [00:36<00:00, 271.84frame/s]
    Downloaded 10000 frames in 36.8s (271 fps, 44.8 MB/s)
    Data check: OK, 10000 frames, range [24.9, 36.2], mean 28.1

Download integrity and recovery
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:meth:`pyTelops.Camera.buffer_download` checks that every frame arrived whole.
By default (``max_dropped_frames=0``) it raises
:exc:`pyTelops.FrameIntegrityError` if any frame is still incomplete after
recovery, so an unnoticed gap cannot slip into your data.  Pass
``max_dropped_frames=N`` to tolerate up to ``N`` incomplete frames and get the
array back anyway:

.. code-block:: python

    from pyTelops import FrameIntegrityError

    try:
        data = cam.buffer_download(sequence=0)
    except FrameIntegrityError as exc:
        print(f"{exc.stats.n_incomplete} frame(s) incomplete")
        # Or tolerate a few drops:
        data = cam.buffer_download(sequence=0, max_dropped_frames=5)

The download self-recovers before it gives up: frames that arrive incomplete or
never arrive are re-streamed at a paced lower bitrate until they are complete,
controlled by ``retries``.  ``resend=`` toggles GVSP packet resends, which are
off by default because resend requests can congest a healthy link.

Auto-tune
~~~~~~~~~

Auto-tune is on by default.  On the first download of a connection it probes
once whether the path carries jumbo frames, using them when supported and
falling back to 1500 otherwise, and it learns a starting bitrate from how
complete each download is, lowering the bitrate after drops.  You do not need to
hand-pick a packet size.  Passing an explicit ``packet_size=`` or
``bitrate_mbps=`` disables auto-tune for that call, and ``cam.auto_tune = False``
disables it entirely:

.. code-block:: python

    data = cam.buffer_download(sequence=0, bitrate_mbps=500)   # manual override

Download statistics
~~~~~~~~~~~~~~~~~~~~

Every call attaches ``cam.last_download_stats``, a
:class:`pyTelops.DownloadStats` with fields such as ``n_frames``,
``n_incomplete``, ``incomplete_frame_ids``, ``throughput_mbps``,
``packet_size_used``, and ``bitrate_used``.  Callers can check transfer quality
without inspecting pixel values:

.. code-block:: python

    data = cam.buffer_download(sequence=0)
    stats = cam.last_download_stats
    print(f"{stats.n_incomplete} incomplete, {stats.throughput_mbps:.1f} MB/s, "
          f"packet_size={stats.packet_size_used}")

If downloads are repeatedly slow or incomplete, :func:`pyTelops.tune_connection`
probes the link and sweeps download settings, returning a
:class:`pyTelops.ConnectionReport`.  Its ``.apply(cam)`` method stores the
recommended configuration on the camera for later downloads:

.. code-block:: python

    from pyTelops import tune_connection

    report = tune_connection(cam)   # camera must have frames recorded first
    report.apply(cam)
    data = cam.buffer_download(sequence=0)

See :doc:`troubleshooting` (buffer-download section) for diagnosing a host or
adapter that cannot keep up at full rate.

External trigger
----------------

For triggered recording from an external BNC signal:

.. code-block:: python

    with Camera() as cam:
        cam.configure_trigger(source="external", activation="rising")

        cam.buffer_configure(n_sequences=1, frames_per_seq=5000,
                             pre_moi=1000,
                             moi_source="external")

        cam.buffer_arm()               # arm and wait for trigger
        cam.buffer_wait(timeout=60.0)  # blocks until recording completes
        data = cam.buffer_download()

For manual control with a software MOI instead of
:meth:`pyTelops.Camera.buffer_record`:

.. code-block:: python

    cam.buffer_arm()
    cam.buffer_fire_moi()
    cam.buffer_wait(timeout=30.0)
    data = cam.buffer_download()

Resolution and frame rate
--------------------------

Reducing the sensor window (subwindow) directly increases the maximum frame
rate.  Width steps are 64 pixels (64--320); height steps are 4 pixels (4--256).
Heights are in usable pixels; the driver adds 2 header rows internally.

.. code-block:: python

    cam.resolution = (128, 64)    # 128x64 pixels
    cam.roi_offset = (96, 96)     # offset within full sensor

    cam.frame_rate_max            # check achievable fps for current settings
    cam.valid_widths              # [64, 128, 192, 256, 320]
    cam.valid_heights             # [4, 8, 12, ..., 252, 256]

Example frame rates at a 10 us integration time:

==========  ===========  =========
Resolution  Int. time    Max FPS
==========  ===========  =========
320x256     10 us        3,115
320x128     10 us        5,973
320x64      10 us        11,034
128x64      10 us        17,836
64x32       10 us        36,676
64x4        10 us        64,491
64x4        5 us         95,184
==========  ===========  =========

.. warning::

   Cycling resolution rapidly (e.g., changing it in a tight loop) can crash the
   camera firmware.  Always allow at least 1 second between resolution changes,
   or power-cycle the camera to recover.  See :doc:`troubleshooting`.
