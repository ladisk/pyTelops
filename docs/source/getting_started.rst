Getting started
===============

Installation
------------

.. code-block:: bash

    pip install pyTelops

For the Tkinter live viewer:

.. code-block:: bash

    pip install pyTelops[gui]

Requirements
------------

* Python 3.10 or newer
* A network interface on the camera's subnet (usually a link-local
  ``169.254.x.x`` address on a dedicated Ethernet adapter)
* Inbound UDP allowed for the Python process (see :doc:`troubleshooting`)

First contact
-------------

.. code-block:: python

    from pyTelops import discover, Camera

    for cam in discover():
        print(cam["manufacturer"], cam["model"], cam["ip"])

    with Camera() as cam:
        cam.calibration_mode = "RT"
        cam.integration_time_auto = "continuous"
        frame = cam.grab()
        print(frame.shape, frame.dtype)

.. warning::

   **VPNs with a link-local adapter (Tailscale) break discovery.**
   pyTelops finds the camera by scanning link-local (``169.254.x.x``)
   interfaces. Tailscale's virtual adapter also holds a ``169.254.x.x``
   address and can shadow the camera's Ethernet adapter, so discovery finds
   nothing. Stop the Tailscale service (and disable its adapter) before
   running, or see :doc:`troubleshooting` for the exact commands.
