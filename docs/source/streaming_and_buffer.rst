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

Pre-trigger recording (pre-MOI)
-------------------------------

Once armed, the camera fills the onboard buffer as a ring and keeps
overwriting it.  The MOI (moment of interest) fixes what is kept: the
``pre_moi`` frames recorded before the MOI, and the rest of the sequence after
it.  An event can therefore be recorded from before it started, as long as the
MOI is fired at the event and not earlier.

``pre_moi`` is a frame count, not a duration.  Convert it with the frame rate:

.. code-block:: python

    cam.frame_rate = 2000.0
    cam.buffer_configure(frames_per_seq=400,
                         pre_moi=int(0.05 * cam.frame_rate),   # 50 ms before
                         moi_source="software")

The MOI source is set in :meth:`pyTelops.Camera.buffer_configure`: a BNC edge
(``"external"``), acquisition start (``"acquisition_started"``) or software
(``"software"``).  All of them honour ``pre_moi``.

With a software MOI there are two ways to fire it.  The manual flow keeps
arming and firing as separate steps:

.. code-block:: python

    cam.buffer_arm()            # the ring starts filling here
    wait_for_my_event()         # your own code: a DAQ callback, input(), ...
    cam.buffer_fire_moi()
    cam.buffer_wait(timeout=30.0)
    data = cam.buffer_download()

:meth:`pyTelops.Camera.buffer_record` is a shortcut over exactly that
sequence, and takes the waiting step as its ``wait_for`` argument.  It accepts
a delay in seconds or a callable that returns at the event:

.. code-block:: python

    cam.buffer_record(wait_for=0.5)                   # fire 0.5 s after arming
    cam.buffer_record(wait_for=lambda: my_event.wait())
    cam.buffer_record(wait_for=input)                 # fire on Enter

The callable runs once per sequence, with no arguments, after arming and
before the MOI is fired.  Its return value is ignored.

Without ``wait_for``, ``buffer_record()`` fires the MOI as soon as the camera
is ready, which is what you want when ``pre_moi`` is 0.  With ``pre_moi > 0``
it warns, because the split point is then placed by a timer and not by an
event, and it first waits until ``pre_moi / frame_rate`` seconds have passed
since arming, so the pre-trigger window is at least full.  Pass ``wait_for=0``
to fire immediately without the warning.

Use ``buffer_record()`` when the recording is self-timed, and the manual flow
when the event and the camera are driven by separate parts of your program.
``examples/08_pretrigger_software_moi.py`` shows both.

Frame headers and timestamps
----------------------------

Every frame carries a 256-byte Telops header in the two metadata rows the
driver normally strips.  The camera writes it at exposure time, so the
timestamp in it is the time the frame was taken, without the transfer and
scheduling delay a host-side clock reading would add.  The header also holds
the frame id, the exposure time, the frame rate and the geometry.

The timestamp comes from the camera clock.  Call
:meth:`pyTelops.Camera.sync_time` before recording to set that clock from the
host, otherwise the absolute time can be off by however far the camera has
drifted.  ``sync_time()`` writes whole seconds, so absolute timestamps are good
to about 1 s.  Differences between frames are exact either way.  Sub-second
alignment to the host clock is not available from the driver yet: the camera
sub-second register is read-only, and ``cam.posix_time = ...`` also writes
whole seconds only.

Pass ``return_headers=True`` to :meth:`pyTelops.Camera.buffer_download` to get
one :class:`pyTelops.FrameHeader` per frame:

.. code-block:: python

    import numpy as np

    cam.sync_time()
    cam.buffer_record(wait_for=0.5)

    data, headers = cam.buffer_download(sequence=0, return_headers=True)
    print(headers[0].datetime)          # 2026-09-08 08:00:00.123456+00:00
    print(headers[0].frame_id, headers[0].frame_rate_hz)

    t = np.array([h.timestamp for h in headers])
    t -= t[0]                           # seconds from the first frame

Entry ``i`` belongs to frame ``i`` of the array.  A frame whose header does not
parse gives ``None`` in the list, and the download warns once with the count.

Parsing builds one Python object per frame.  For a download of tens of
thousands of frames, take the raw frames and use the vectorised helpers
instead:

.. code-block:: python

    from pyTelops import header_timestamps, header_frame_ids

    raw = cam.buffer_download(sequence=0, strip_header=False, convert=False)
    t = header_timestamps(raw, relative=True)   # float64 seconds from frame 0
    ids = header_frame_ids(raw)                 # uint32 frame ids

To find the moment of interest in a recording, ask the camera:

.. code-block:: python

    moi = cam.buffer_moi_index(0)       # index of the MOI frame in the download
    if 0 <= moi < len(headers):
        event_time = headers[moi].timestamp

The camera reports the MOI as a frame id in its own register id space.  How
that id maps onto the download index is not yet verified on hardware, so treat
``buffer_moi_index()`` as an estimate and check the bounds before you use it.
A non-default ``start_frame`` shifts the index as well.

From header version 12.9 on, each header also flags its own position with
:class:`pyTelops.BufferingFlag` (``PRE_MOI``, ``MOI``, ``POST_MOI``):

.. code-block:: python

    print(headers[0].header_version)    # the FAST-M3k reports device XML 12.7,
                                        # so expect (12, 7)
    print(headers[0].buffering_flag)    # None below 12.9

On header 12.7 byte 74 is still reserved, so ``buffering_flag`` is ``None``
there and ``buffer_moi_index()`` is the way to locate the event.

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
:meth:`pyTelops.Camera.buffer_record`, fire the MOI at the event so the
``pre_moi`` frames before it are the ones you want:

.. code-block:: python

    cam.buffer_arm()
    wait_for_my_event()          # your own code returns at the event
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
