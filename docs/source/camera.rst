Camera API reference
====================

.. currentmodule:: pyTelops

.. autoclass:: Camera
   :members:
   :undoc-members:
   :show-inheritance:

Module-level helpers
--------------------

.. autofunction:: discover

Provisioning and tuning
-----------------------

.. autofunction:: force_ip

.. autofunction:: tune_connection

Errors and results
------------------

.. autoclass:: ConnectionReport
   :members:

.. autoclass:: DownloadStats
   :members:

.. autoexception:: FrameIntegrityError
   :members:

Frame headers
-------------

.. autoclass:: FrameHeader
   :members:

.. autoclass:: BufferingFlag

.. autofunction:: parse_header

.. autofunction:: parse_headers

.. autofunction:: header_timestamps

.. autofunction:: header_frame_ids

Enumerations
------------

.. automodule:: pyTelops.registers
   :members:
   :undoc-members:
   :show-inheritance:
