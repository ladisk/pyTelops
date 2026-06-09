Contributing to pyTelops
========================

Thanks for your interest in contributing. This guide covers the development
workflow.

Architecture
------------

pyTelops is the vendor layer on top of `pyGigEVision
<https://github.com/ladisk/pyGigEVision>`_, which implements the GigE Vision
protocol (GVCP control, GVSP streaming, GenICam XML). pyTelops adds only the
Telops-specific parts:

- ``camera.py`` - the ``Camera`` class: properties, context manager,
  auto-discovery and connect, RT-mode Celsius conversion, and 16 GB onboard
  buffer recording with integrity-checked download.
- ``registers.py`` - Telops register addresses and enums.
- ``connection.py`` - ``tune_connection`` / ``ConnectionReport`` (link probing
  and download tuning).
- ``errors.py`` - ``DownloadStats`` and ``FrameIntegrityError``.
- ``provisioning.py`` - ``force_ip`` (assign a camera IP by MAC).
- ``cli.py`` / ``gui.py`` - command-line tools and the Tkinter live viewer.

Control runs over GVCP (UDP 3956, request/response with a heartbeat); frames
arrive over GVSP (the camera pushes Leader / Data / Trailer packets that are
reassembled into numpy arrays). Both are provided by pyGigEVision.

Development setup
-----------------

1. Fork and clone:

   .. code-block:: bash

      git clone https://github.com/<your-username>/pyTelops.git
      cd pyTelops

2. Install in editable mode with dev (and GUI) extras:

   .. code-block:: bash

      pip install -e ".[dev,gui]"

   This pulls in pytest, sphinx, ruff, and the rest of the dev tools.

   Note: pyTelops depends on `pyGigEVision
   <https://github.com/ladisk/pyGigEVision>`_. While that package is
   pre-release the dependency resolves over SSH from GitHub; once it is on
   PyPI the pin becomes a normal version.

3. Create a feature branch off ``master``:

   .. code-block:: bash

      git checkout -b feature/my-change

Making a change
---------------

1. Make your change.
2. Add or update tests in ``tests/``. Hardware-only tests use the
   ``@pytest.mark.hardware`` marker and run with ``pytest --hardware``.
3. Add or update NumPy-style docstrings on every public symbol you touch.
4. Run the local checks:

   .. code-block:: bash

      pytest
      ruff check pyTelops tests
      ruff format --check pyTelops tests

5. Add a line to ``docs/source/changelog.rst`` under the next version.
6. Push to your fork and open a pull request against ``master``.

Running hardware tests
----------------------

The 57 hardware tests need a connected Telops camera:

.. code-block:: bash

   pytest tests/test_hardware.py --hardware

Stop any VPN that owns a link-local adapter first, or
discovery will fail. See the troubleshooting guide.

Code style
----------

* Line length 100, enforced by ``ruff format``.
* Modern type hints (``str | None``, ``list[dict]``) with
  ``from __future__ import annotations`` at the top of each module.
* f-strings, not ``.format()``. No bare ``except:``.

Documentation
-------------

Built with Sphinx, hosted on ReadTheDocs.

.. code-block:: bash

   pip install -r docs/requirements.txt
   sphinx-build -b html docs/source docs/build/html

Setting up ReadTheDocs (one-time, maintainers)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

1. Sign in to https://readthedocs.org with the maintainer GitHub account.
2. Import ``ladisk/pyTelops``.
3. Set the slug to ``pytelops``.
4. Trigger the first build.

Releasing (maintainers)
-----------------------

1. Make sure ``master`` is green.
2. Update ``docs/source/changelog.rst``.
3. Tag and push:

   .. code-block:: bash

      git tag vX.Y.Z
      git push origin vX.Y.Z

4. The ``release-and-publish-to-pypi.yml`` workflow syncs the version into
   ``pyproject.toml``, ``pyTelops/__init__.py``, and ``docs/source/conf.py``,
   builds, publishes to PyPI using the ``PYPI_API_TOKEN`` secret, and creates
   a GitHub Release.

License
-------

By contributing you agree your contribution is licensed under the MIT License.
