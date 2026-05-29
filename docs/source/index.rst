pyTelops
========

Pure-Python driver for `Telops <https://www.telops.com/>`_ thermal cameras
over GigE Vision. No vendor SDK required: pyTelops speaks the GVCP and GVSP
protocols directly over UDP, on top of `pyGigEVision
<https://github.com/ladisk/pyGigEVision>`_.

Quickstart
----------

.. code-block:: python

    from pyTelops import Camera

    with Camera() as cam:
        cam.calibration_mode = "RT"
        cam.integration_time_auto = "continuous"
        frame = cam.grab()          # calibrated Celsius frame, headers stripped

See :doc:`getting_started` for installation and first contact.

Contents
--------

.. toctree::
   :maxdepth: 2

   getting_started
   camera
   calibration
   streaming_and_buffer
   examples
   troubleshooting
   changelog

Indices and tables
------------------

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
