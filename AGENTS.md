# pyTelops for agents

pyTelops is a pure-Python driver for Telops thermal cameras over GigE Vision
(no vendor SDK). It speaks GVCP and GVSP directly over UDP on top of
[pyGigEVision](https://github.com/ladisk/pyGigEVision).

This file is for coding agents. For the full human guide see
[CONTRIBUTING.rst](CONTRIBUTING.rst); the commands here mirror it.

## Documentation

Published at https://pytelops.readthedocs.io/en/latest/

Start at https://pytelops.readthedocs.io/en/latest/llms.txt for the page index,
or fetch https://pytelops.readthedocs.io/en/latest/llms-full.txt for the whole
corpus in one request. Every page is also available as Markdown by replacing
the `.html` extension with `.md`.

| Doing | Read |
|---|---|
| Connecting and grabbing a frame | getting_started.md |
| Full API surface | camera.md |
| Calibration modes (RT / NUC / RAW), loading calibrations | calibration.md |
| Live streaming and onboard-buffer recording | streaming_and_buffer.md |
| Runnable scripts | examples.md |
| Discovery / network / firewall problems | troubleshooting.md |

## Setup

```bash
pip install -e ".[dev,gui]"
```

This pulls the dev tools (pytest, ruff, sphinx) and the optional GUI extras.

Note: while pyGigEVision is pre-release, this resolves it over SSH from GitHub
(`git+ssh://git@github.com/ladisk/pyGigEVision`), so the install needs an SSH
key with access to that repo. Once pyGigEVision is on PyPI the pin becomes a
normal version and no SSH access is required.

## Test, lint, format

```bash
pytest                                 # unit tests; hardware tests auto-skip
ruff check pyTelops tests
ruff format --check pyTelops tests
```

Hardware tests are gated behind a flag and need a connected Telops camera:

```bash
pytest tests/test_hardware.py --hardware
```

Stop any VPN that holds a link-local adapter (e.g. Tailscale) before running
hardware tests, or camera discovery fails. See troubleshooting.md.

## Conventions

- Line length 100, enforced by `ruff format` (double quotes).
- `from __future__ import annotations` at the top of each module; modern type
  hints (`str | None`, `list[dict]`).
- f-strings, not `.format()`. No bare `except:`.
- NumPy-style docstrings on every public symbol.
- No em dashes in user-facing docs, docstrings, or changelogs.
- Add a line to `docs/source/changelog.rst` under the next version for any
  user-visible change.
- Branch off `master`; open pull requests against `master`.

## Project layout

```
pyTelops/        driver package: camera.py, registers.py, cli.py, gui.py
tests/           pytest suite (test_hardware.py is camera-gated)
examples/        runnable scripts (01_connect_and_grab.py ... 05_external_trigger.py)
docs/source/     Sphinx docs (RST source)
```

The `pytelops` console command is the CLI entry point (`pyTelops.cli:main`).
Requires Python 3.10 or newer.
