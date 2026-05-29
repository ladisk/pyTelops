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
Packet delay has no effect on buffer recording or buffer download.

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
    Data check: OK -- 10000 frames, range [24.9--36.2], mean 28.1

If the download stalls or fails the integrity check, see
:doc:`troubleshooting` (buffer-download section) -- the most common cause is
competing network load from a running video-call app.  You can also lower the
download bitrate to leave headroom:

.. code-block:: python

    data = cam.buffer_download(sequence=0, bitrate_mbps=500)

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
