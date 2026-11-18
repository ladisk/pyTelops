MCP server
==========

``pytelops mcp`` runs a Model Context Protocol (MCP) server for the camera.
An AI assistant such as Claude Code can then connect to the camera, change
settings, grab frames, and record and download buffer sequences.
The server talks over stdio. It controls one camera.

Install
-------

Install the ``mcp`` extra:

.. code-block:: bash

   pip install "pyTelops[mcp]"

The extra needs the official ``mcp`` package, version 2.3 or later.

Set up Claude Code
------------------

Register the server and choose a data folder:

.. code-block:: bash

   claude mcp add telops -- pytelops mcp --data-dir ~/takes/telops

The default data folder is ``./pytelops-data``. The server creates it when it
saves the first file. The server finds the camera by network discovery at each
connect. Other MCP clients use the same command.

Tools
-----

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - Tool
     - Action
   * - ``camera_doctor``
     - Check host, network, and camera. ``stream=true`` also tests frame delivery. It needs the camera disconnected.
   * - ``camera_connect``
     - Find and connect to the camera. Runs as a job. Optional ``ip``.
   * - ``camera_disconnect``
     - Disconnect and release control.
   * - ``camera_status``
     - Connection, settings, sensor temperature, buffer state, and current job.
   * - ``camera_configure``
     - Set only the given fields: size, offset, integration time, frame rate, calibration mode. Returns the actual values and any clamps.
   * - ``camera_grab``
     - Grab 1 to 500 frames. Saves ``.npy``, a PNG preview, and JSON. Returns statistics and the preview image.
   * - ``buffer_record``
     - Record into the camera memory. Runs as a job. Refuses if the memory holds recordings.
   * - ``buffer_download``
     - Download one recorded sequence to a raw ``.npy`` file. Runs as a job.
   * - ``buffer_clear``
     - Delete all recordings in the camera memory. Needs ``confirm=true``.
   * - ``camera_nuc``
     - Run a non-uniformity correction. Needs ``confirm=true``. Takes up to 60 s.
   * - ``job_status``
     - State, progress, message, and result or error of a job.
   * - ``job_cancel``
     - Ask a job to stop.

Jobs and polling
----------------

Connect, record, and download can take minutes. They run as jobs in a
background thread. The tool returns a ``job_id`` at once.

Poll ``job_status`` with that ``job_id``. The state is ``running``, ``done``,
``failed``, or ``cancelled``. A finished job holds a ``result`` or an ``error``.

Only one job runs at a time. While a job runs, other camera tools refuse and
name the running job. ``job_status``, ``job_cancel``, and ``camera_status``
still work.

``camera_status`` does not read the camera during a job. Some buffer reads
write camera registers. The tool returns the snapshot from the last finished
call and sets ``live`` to ``false``.

``job_cancel`` stops a job at its next check:

- Record stops within about 0.5 s and stops the acquisition.
- Download stops after the current slice of up to 1000 frames.
- Connect stops during the cooldown wait, within about 3 s.

``camera_connect`` returns the result at once if the camera is ready. If the
camera is still cooling, the tool returns a job. A cold camera can need up to
10 minutes.

Data layout
-----------

.. code-block:: text

   <data dir>/
     grabs/
       grab_20261118_101500.npy    frames, shape (N, H, W)
       grab_20261118_101500.png    preview of the first frame
       grab_20261118_101500.json   settings and statistics
     downloads/
       download_20261118_102000.npy    uint16, shape (N, H+2, W)
       download_20261118_102000.json   metadata

Names come from the time in UTC. Use the ``name`` argument to choose a name.
A name can hold letters, digits, ``.``, ``_``, and ``-``. The server never
overwrites a file.

A download keeps the two header rows of each frame and the raw uint16 values.
The server writes the file slice by slice. The JSON file shows ``complete``,
``state``, ``frames_written``, and ``integrity_ok``. If a download is
cancelled or fails, the ``.npy`` file is partial. Frames after
``frames_written`` are zeros.

``integrity_ok`` is ``false`` if frames are missing, if any frame ID differs from the requested
range, or if the driver counted out-of-order or strided frames. Download the sequence again in that case.

Convert a download to Celsius
-----------------------------

Each frame carries its own conversion data in the header rows. In RT mode
(``calibration_mode`` 2), convert each frame:

.. code-block:: python

   import numpy as np
   from pyTelops import parse_header

   raw = np.load("downloads/rec1.npy", mmap_mode="r")
   frame = np.asarray(raw[0])
   h = parse_header(frame)
   celsius = frame[2:].astype(np.float32) * 2.0**h.data_exp + h.data_offset - 273.15

In NUC and RAW modes, ``data_exp`` and ``data_offset`` are zero. The values
are counts. The JSON ``header`` block holds the values of the first frame.
Read the header of each frame if the integration time or the calibration
changed during the recording.

Safety rules
------------

- ``buffer_clear`` and ``camera_nuc`` do nothing without ``confirm=true``. The
  assistant must ask you first.
- For a ``black_body`` NUC, put a uniform black body or a lens cap in front of
  the lens before you confirm.
- ``buffer_record`` refuses to run while the camera memory holds recordings.
  Download them, then call ``buffer_clear`` with ``confirm=true``.
- Buffer mode stays on after a recording. ``camera_grab`` and ``camera_configure``
  turn it off. If the memory still holds recordings, they refuse. Download and clear first.
- Only one client controls the camera. If another program holds it, connect
  fails with ``ACCESS_DENIED``. Close the other program and try again. A lock
  from a crashed session expires after about 15 s.
- Do not use the Telops software, ``pytelops live``, or a Python session at
  the same time.
- If registers stay locked or frames stop, power-cycle the camera.
- The server stops the running job and disconnects when the client closes
  stdin or when the process gets SIGINT or SIGTERM.

The server never writes to stdout, because stdout carries the protocol.
Driver messages go to stderr.

Simulate mode
-------------

Start the server with ``--simulate`` to test without a camera:

.. code-block:: bash

   pytelops mcp --simulate --data-dir /tmp/telops-sim

The simulated camera makes synthetic frames with header rows. It supports all
tools. Frame IDs start at 1000, as on the real camera.
