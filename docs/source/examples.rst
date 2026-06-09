Examples
========

Seven runnable scripts ship in the ``examples/`` directory. Each needs a
connected Telops camera.

01_connect_and_grab.py
----------------------

Discover cameras, connect, set RT calibration mode and auto integration time,
grab one calibrated frame and a short batch.

02_continuous_live.py
---------------------

Continuous acquisition for a live display. Uses
:meth:`~pyTelops.Camera.read_frame` with ``latest=True`` so the shown frame
never lags behind real time.

03_buffer_record.py
-------------------

Configure and record one sequence to the onboard 16 GB buffer, then download
it. See :doc:`streaming_and_buffer` for the streaming-versus-buffer trade-off.

04_calibration_load.py
----------------------

Load calibration info from the camera's USB folder, list the collections, and
select one by lens and target temperature. See :doc:`calibration`.

05_external_trigger.py
----------------------

Arm the buffer and record when an external BNC trigger fires.

06_force_ip.py
--------------

Discover cameras, find one reported as not reachable (on no host subnet), and
assign it a new IP by MAC with :func:`~pyTelops.force_ip`, then re-discover.
See :doc:`troubleshooting`.

07_robust_download.py
---------------------

Record a sequence, tune the link with :func:`~pyTelops.tune_connection`, then
download with integrity checking and inspect
:attr:`~pyTelops.Camera.last_download_stats`. See :doc:`streaming_and_buffer`.
