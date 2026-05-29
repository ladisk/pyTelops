Calibration
===========

Each lens and temperature range has its own calibration dataset stored on the
camera. Before acquiring calibrated frames in RT mode, you load the correct
dataset from the USB drive shipped with the camera.

Loading calibration info
------------------------

Point :meth:`pyTelops.Camera.load_calibration_info` at the root folder of the
calibration USB drive:

.. code-block:: python

    cam.load_calibration_info("path/to/TEL-8050 Calibration Data/")

This reads the ``.tsco`` files and exposure tables from the drive into memory.
It does not yet activate any calibration on the camera -- it just makes the
collections available for inspection and selection.

Listing available collections
------------------------------

:meth:`pyTelops.Camera.calibration_collections` returns a list of dicts, one
per calibration collection found on the USB drive:

.. code-block:: python

    cam.calibration_collections()
    # [{'index': 0, 'lens': 'MW Microscope 1X', 'temp_range': (0, 204), ...},
    #  {'index': 4, 'lens': 'MW 25mm',          'temp_range': (0, 184), ...},
    #  {'index': 8, 'lens': 'MW 50mm',          'temp_range': (0, 175), ...}, ...]

Each entry includes the collection index (zero-based), the lens label, and the
temperature range it covers in degrees Celsius.

Selecting a calibration
-----------------------

Use :meth:`pyTelops.Camera.calibration_load` to activate a collection.  You
can select by lens name plus a target temperature, or directly by index:

.. code-block:: python

    # Select the 50 mm lens collection covering the 0-175 C range
    cam.calibration_load(lens="50mm", temp=25)

    # Select the 25 mm lens collection covering the 115-376 C range
    cam.calibration_load(lens="25mm", temp=300)

    # Select directly by zero-based collection index
    cam.calibration_load(index=4)

When selecting by ``lens`` and ``temp``, the method picks the collection whose
temperature range contains ``temp``.  If multiple collections match (overlapping
ranges), the one with the narrowest temperature range is chosen.

Checking the active collection
--------------------------------

:meth:`pyTelops.Camera.calibration_active` returns information about the
collection currently loaded onto the camera:

.. code-block:: python

    cam.calibration_active()
    # {'index': 8, 'lens': 'MW 50mm', 'temp_range': (0, 175), ...}

File naming conventions
-----------------------

The calibration USB drive uses a specific naming scheme that the driver
normalises internally:

- ``.tsco`` files -- temperature-to-signal conversion objects, one per
  temperature sub-range within a collection.
- Exposure files -- named with an ``ELSN`` prefix on the drive; the driver
  normalises these to ``EL`` internally.
- Collection indices are zero-based in the driver API, but the filenames on the
  USB drive use one-based numbering.  You do not need to account for this
  offset: pass the index as shown by
  :meth:`pyTelops.Camera.calibration_collections`.

Image correction (NUC)
----------------------

A Non-Uniformity Correction (NUC) compensates for per-pixel offset and gain
variation in the sensor.  Trigger it programmatically after the camera has
reached a stable operating temperature:

.. code-block:: python

    cam.nuc()                           # one-point NUC (blocks until done)
    cam.nuc(mode="icu")                 # using the internal calibration unit
    cam.nuc(blackbody_temp=25.0)        # with a blackbody reference temperature

``cam.nuc()`` blocks until the camera reports the correction is complete.
Running a NUC with a shutter closed or the lens covered produces an invalid
correction -- ensure the camera has a clear view (or use ``mode="icu"`` for the
internal shutter).
