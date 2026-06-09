"""Download from the onboard buffer robustly, with optional link tuning.

A plain ``buffer_download()`` is already integrity-checked, self-recovering, and
auto-tuned: it probes the path and learns a bitrate on its own, and by default
raises ``FrameIntegrityError`` if any frame is incomplete. This script shows how
to tolerate a few drops and read ``cam.last_download_stats``, plus how to
escalate to ``tune_connection`` when a link is stubbornly unreliable.

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

        # Optional, only for a stubbornly unreliable link. buffer_download
        # already auto-tunes on its own, so most setups can skip this. When the
        # link is flaky, tune_connection sweeps settings once and apply() stores
        # the winner so later downloads reuse it.
        report = tune_connection(cam)
        report.apply(cam)
        print("Recommended download config:", report.recommended)

        # max_dropped_frames=0 (the default) raises on any gap; here we tolerate
        # a few and read the integrity report instead.
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
