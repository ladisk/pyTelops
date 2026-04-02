.. image:: banner.png
   :alt: pyTelops — Pure-Python Telops Thermal Camera Driver

pyTelops
========

.. image:: https://img.shields.io/pypi/v/pyTelops.svg
   :target: https://pypi.org/project/pyTelops/

.. image:: https://img.shields.io/pypi/l/pyTelops.svg
   :target: https://github.com/jasasonc/pyTelops/blob/main/LICENSE

Pure-Python driver for `Telops <https://www.telops.com/>`_ thermal cameras
over GigE Vision. No vendor SDK required — communicates directly via GVCP/GVSP
protocols over UDP.

Supported cameras:

- Telops FAST M3k (tested)
- Other Telops GigE Vision cameras (should work, untested)

Features
--------

- **Auto-discovery** — finds cameras on the network regardless of IP
- **Live streaming** — real-time frame acquisition via GVSP (up to ~760 fps at full resolution)
- **Internal buffer** — record at full sensor speed (up to 3100 fps at full frame), download at ~270 fps
- **Camera control** — exposure, frame rate, calibration mode, trigger, resolution
- **String enums** — ``cam.calibration_mode = "RT"`` instead of importing enum classes
- **Auto header stripping** — frames are returned without Telops metadata rows
- **GUI viewer** — live thermal image display with colormap selection
- **CLI tools** — ``pytelops discover``, ``pytelops setup``, ``pytelops grab``, ``pytelops live``
- **Progress bars** — tqdm progress for buffer downloads
- **Data integrity checks** — automatic diagnostics after download
- **Robust connection** — auto-recovers from stale sessions, handles ACCESS_DENIED
- **Pure Python** — only requires numpy and tqdm

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
       cam.calibration_mode = "RT"      # radiometric temperature
       cam.exposure_auto = "continuous"  # auto exposure (or "off" for manual)
       # cam.exposure = 50.0            # set manually when exposure_auto is "off"

       frame = cam.grab()               # single frame -> numpy (H, W)
       frames = cam.acquire(10)         # 10 frames -> numpy (N, H, W)

Frames are returned as numpy arrays with Telops header rows already stripped.

Streaming vs buffer
-------------------

pyTelops supports two acquisition modes:

**Live streaming** — frames stream directly to PC over Ethernet.
Limited by GigE bandwidth (~125 MB/s); up to ~760 fps at full frame (320×256):

.. code-block:: python

   frame = cam.grab()             # single frame
   frames = cam.acquire(100)      # 100 frames

**Buffer recording** — the camera records to its internal 16 GB memory at full
sensor speed (up to 3100 fps at full frame), then downloads to PC:

.. code-block:: python

   cam.frame_rate = 2000.0
   cam.buffer_configure(n_sequences=3, duration=5.0, moi_source="software")

   cam.buffer_record()   # records all 3 sequences automatically
   cam.buffer_info()     # check what was recorded
   data = cam.buffer_download(sequence=0)   # download one sequence
   cam.buffer_clear()

Use streaming for low frame rate continuous capture. Use the buffer for
high-speed measurements where you need thousands of fps.

Buffer recording
----------------

The buffer must be partitioned into fixed-size sequence slots before recording:

.. code-block:: python

   from pyTelops import Camera

   with Camera() as cam:
       cam.frame_rate = 2000.0
       cam.exposure = 30.0

       # Allocate: 3 sequences of 5 seconds each (uses current frame_rate)
       cam.buffer_configure(n_sequences=3, duration=5.0,
                            moi_source="software")

       # Record all sequences in one call
       cam.buffer_record()   # arms, fires MOI for each, waits, stops

       # Review
       print(cam.buffer_info())
       # {'status': 'IDLE', 'n_sequences': 3, 'recorded': [10000, 10000, 10000], ...}

       # Download selected sequences
       data_0 = cam.buffer_download(sequence=0)
       data_2 = cam.buffer_download(sequence=2)

       # Clean up
       cam.buffer_clear()

``buffer_record()`` prints progress::

   Arming (seq 1/3)... Recording... Done (10000 frames)
   Firing (seq 2/3)... Recording... Done (10000 frames)
   Firing (seq 3/3)... Recording... Done (10000 frames)

``buffer_download()`` shows a tqdm progress bar and data integrity check::

   Downloading: 100%|██████████| 10000/10000 [00:36<00:00, 271.84frame/s]
   Downloaded 10000 frames in 36.8s (271 fps, 44.8 MB/s)
   Data check: OK — 10000 frames, range [6908–65534], mean 8671

External trigger
----------------

For triggered recording from an external BNC signal:

.. code-block:: python

   with Camera() as cam:
       cam.configure_trigger(source="external", activation="rising")

       cam.buffer_configure(n_sequences=1, frames_per_seq=5000,
                            pre_moi=1000,
                            moi_source="external")

       cam.buffer_arm()                  # arm and wait for trigger
       cam.buffer_wait(timeout=60.0)     # blocks until recording completes
       data = cam.buffer_download()

Camera configuration
--------------------

All settings are properties with string enum support:

.. code-block:: python

   cam.exposure = 50.0                   # microseconds
   cam.frame_rate = 2000.0               # Hz (warns if above max)
   cam.frame_rate_max                    # max Hz for current resolution/exposure
   cam.calibration_mode = "RT"           # "RT", "NUC", "RAW", "IBR", "IBI"
   cam.exposure_auto = "continuous"       # "off", "once", "continuous"
   cam.resolution                        # (320, 258)
   cam.temperature                       # sensor temperature in Celsius
   cam.info                              # dict with all settings
   cam.state                             # "disconnected", "connected", "streaming"

Live viewer
-----------

.. code-block:: python

   with Camera() as cam:
       cam.live_view()

Or from the command line:

.. code-block:: bash

   pytelops live

Opens a Tkinter window with real-time thermal display, percentile
normalization (handles hot pixels), and colormap selector.

CLI
---

.. code-block:: bash

   pytelops discover     # find cameras on the network
   pytelops info         # show camera configuration
   pytelops grab -o frame.npy   # grab a single frame
   pytelops live         # open live viewer
   pytelops setup        # configure OS (firewall, MTU)

Network setup
-------------

GigE Vision requires a firewall rule to allow inbound UDP from the camera:

**Windows** (run once as admin):

.. code-block:: bash

   netsh advfirewall firewall add rule name="pyTelops-GVSP" dir=in action=allow protocol=UDP program="C:\path\to\python.exe"

**Linux**:

.. code-block:: bash

   sudo sysctl -w net.core.rmem_max=16777216

Or use the built-in setup helper:

.. code-block:: bash

   pytelops setup

Integration
-----------

pyTelops is designed to be used standalone or as a backend for data acquisition
frameworks:

- `openEOL <https://github.com/ladisk/openEOL>`_ — industrial end-of-line testing
- `LDAQ <https://github.com/ladisk/LDAQ>`_ — lightweight data acquisition

License
-------

MIT
