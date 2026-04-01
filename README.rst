pyTelops
========

.. image:: https://img.shields.io/pypi/v/pyTelops.svg
   :target: https://pypi.org/project/pyTelops/

.. image:: https://img.shields.io/pypi/l/pyTelops.svg
   :target: https://github.com/ladisk/pyTelops/blob/main/LICENSE

Pure-Python driver for `Telops <https://www.telops.com/>`_ thermal cameras
over GigE Vision. No vendor SDK required — communicates directly via GVCP/GVSP
protocols over UDP.

Supported cameras:

- Telops FAST M3k (tested)
- Other Telops GigE Vision cameras (should work, untested)

Features
--------

- **Auto-discovery** — finds cameras on the network automatically
- **Live streaming** — real-time frame acquisition via GVSP
- **Internal buffer** — configure, record, and download from the 16GB onboard memory
- **Camera control** — exposure, frame rate, calibration mode, trigger, resolution
- **GUI viewer** — live thermal image display with colormap selection
- **CLI tools** — ``pytelops discover``, ``pytelops setup``, ``pytelops grab``
- **Pure Python** — only requires numpy, no compiled extensions or vendor SDK

Installation
------------

.. code-block:: bash

   pip install pyTelops

For the GUI viewer:

.. code-block:: bash

   pip install pyTelops[gui]

Quick start
-----------

.. code-block:: python

   from pyTelops import Camera

   with Camera() as cam:
       print(cam.info)

       cam.exposure = 50.0       # microseconds
       cam.frame_rate = 100.0    # Hz

       frame = cam.grab()        # single frame -> numpy array
       frames = cam.acquire(100) # batch -> (N, H, W) array

Buffer recording
----------------

.. code-block:: python

   from pyTelops import Camera
   from pyTelops.registers import MemoryBufferMOISource

   with Camera() as cam:
       # Configure buffer: 1 sequence, 1000 frames, software trigger
       cam.buffer_configure(n_sequences=1, frames_per_seq=1000,
                            moi_source=MemoryBufferMOISource.SOFTWARE)

       # Record
       cam.buffer_arm()
       # ... wait for event ...
       cam.buffer_fire_moi()

       # Download
       print(f"Recorded: {cam.buffer_recorded_frames()} frames")
       data = cam.buffer_download()  # numpy array (N, H, W)

Live viewer
-----------

.. code-block:: python

   from pyTelops import Camera

   with Camera() as cam:
       cam.live_view()

Or from the command line:

.. code-block:: bash

   pytelops live

CLI
---

.. code-block:: bash

   pytelops discover     # find cameras on the network
   pytelops info         # show camera configuration
   pytelops grab -o frame.npy   # grab a single frame
   pytelops live         # open live viewer
   pytelops setup        # configure OS (firewall, MTU)

Integration
-----------

pyTelops is designed to be used standalone or as a backend for data acquisition
frameworks:

- `openEOL <https://github.com/ladisk/openEOL>`_ — industrial end-of-line testing
- `LDAQ <https://github.com/ladisk/LDAQ>`_ — lightweight data acquisition

License
-------

MIT
