pyTelops
========

Pure-Python driver for Telops thermal cameras over GigE Vision. No vendor SDK
required: pyTelops speaks the GVCP and GVSP protocols directly over UDP.

It builds on `pyGigEVision <https://github.com/ladisk/pyGigEVision>`_ for the
GigE Vision protocol layer and adds the Telops-specific calibration, registers,
and onboard-buffer support.

* GitHub: https://github.com/ladisk/pyTelops
* Documentation: https://pytelops.readthedocs.io

Installation
------------

.. code-block:: bash

    pip install pyTelops

For the Tkinter live viewer:

.. code-block:: bash

    pip install pyTelops[gui]

License: MIT.

pyTelops is an independent project, not affiliated with or endorsed by
Telops Inc. "Telops", "FAST", and "Reveal IR" are trademarks of Telops Inc.
