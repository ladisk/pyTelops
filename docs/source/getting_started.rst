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
        state = "reachable" if cam["reachable"] else "not reachable"
        print(cam["manufacturer"], cam["model"], cam["ip"], state)

    with Camera() as cam:
        cam.calibration_mode = "RT"
        cam.integration_time_auto = "continuous"
        frame = cam.grab()
        print(frame.shape, frame.dtype)

``discover()`` searches every host network interface, so cameras on a
secondary NIC or a USB-to-GigE adapter are found without any manual interface
selection. Each result is a dict carrying ``manufacturer``, ``model``, ``ip``,
``serial``, ``mac``, ``interface_ip`` (the host interface the camera replied
on), and ``reachable``. Pass ``all_vendors=True`` to list non-Telops GigE
Vision devices as well.

``Camera()`` connects through the interface the camera replied on during
discovery, so no manual interface selection is needed on hosts with several
NICs. If the chosen camera is on no host subnet (its discovery entry has
``reachable == False``), connecting raises an actionable error. In that case
use :func:`~pyTelops.force_ip` to move the camera onto a reachable subnet; see
:doc:`troubleshooting` for the full procedure.

.. warning::

   **A VPN with a link-local adapter can break discovery.**
   pyTelops finds the camera by scanning link-local (``169.254.x.x``)
   interfaces. A VPN that creates its own virtual link-local adapter can hold
   a ``169.254.x.x`` address and shadow the camera's Ethernet adapter, so
   discovery finds nothing. If discovery returns no cameras, stop the VPN
   service (and disable its virtual adapter), then retry. See
   :doc:`troubleshooting` for more detail.
