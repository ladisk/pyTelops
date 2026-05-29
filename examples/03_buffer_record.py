"""Record to the onboard buffer and download.

The camera records to its 16 GB internal buffer at full sensor speed, then
downloads over Ethernet.

Run with::

    python examples/03_buffer_record.py
"""

from __future__ import annotations

import numpy as np

from pyTelops import Camera


def main() -> None:
    with Camera() as cam:
        cam.frame_rate = 2000.0
        cam.integration_time_auto = "continuous"

        cam.buffer_configure(n_sequences=1, duration=5.0, moi_source="software")
        print(cam.buffer_info())

        cam.buffer_record()

        data = cam.buffer_download(sequence=0)
        if data is not None:
            print(f"Downloaded: {data.shape}, {data.dtype}")
            np.save("buffer_data.npy", data)

        cam.buffer_clear()


if __name__ == "__main__":
    main()
