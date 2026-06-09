"""Record to the onboard buffer, tune the link, and download robustly.

``tune_connection`` probes the link and sweeps download settings, then
``buffer_download`` checks integrity. By default it raises
``FrameIntegrityError`` on incomplete data; here we tolerate a few drops and
inspect ``cam.last_download_stats`` instead.

Run with::

    python examples/07_robust_download.py
"""

from __future__ import annotations

import numpy as np

from pyTelops import Camera, FrameIntegrityError, tune_connection


def main() -> None:
    with Camera() as cam:
        cam.frame_rate = 2000.0
        cam.integration_time_auto = "continuous"

        cam.buffer_configure(n_sequences=1, duration=5.0, moi_source="software")
        cam.buffer_record()

        # Probe the link and store a recommended download config on the camera.
        report = tune_connection(cam)
        report.apply(cam)
        print("Recommended download config:", report.recommended)

        try:
            data = cam.buffer_download(max_dropped_frames=5)
        except FrameIntegrityError as exc:
            print(f"Too many bad frames: {exc.stats.n_incomplete}")
            return

        stats = cam.last_download_stats
        print(
            f"Downloaded {data.shape} at {stats.throughput_mbps:.1f} MB/s, "
            f"{stats.n_incomplete} incomplete frame(s), "
            f"packet_size={stats.packet_size_used}"
        )
        np.save("buffer_data.npy", data)

        cam.buffer_clear()


if __name__ == "__main__":
    main()
