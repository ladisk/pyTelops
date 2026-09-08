"""Keep frames from before an event, with a software MOI (pre-trigger).

Once armed, the camera fills its 16 GB buffer as a ring. The MOI (moment of
interest) marks where the recording is split: ``pre_moi`` frames before it and
the rest after it are kept. So the MOI has to be fired at the event, not right
after arming.

Two ways to do that, both shown below:

1. the manual flow, ``buffer_arm()`` -> wait for the event ->
   ``buffer_fire_moi()`` -> ``buffer_wait()``,
2. ``buffer_record(wait_for=...)``, which does the same in one call.

Both download with ``return_headers=True``, so they can print the camera
timestamps of the recording and where the camera says the MOI sits. The camera
reports the MOI as a frame id in its own id space; how that id maps onto the
download index is not yet verified on hardware, so the index is checked against
the download before it is used.

Run with::

    python examples/08_pretrigger_software_moi.py
"""

from __future__ import annotations

import numpy as np

from pyTelops import Camera, FrameHeader

FRAMES_PER_SEQ = 400
PRE_MOI = 100  # frames kept before the event
FRAME_RATE = 2000.0


def report(cam: Camera, data: np.ndarray, headers: list[FrameHeader | None]) -> None:
    """Print what was recorded, when, and where the event sits in it.

    The timestamps come from the camera clock, set from the host by
    ``sync_time()`` before recording. ``sync_time()`` writes whole seconds, so
    the absolute time is good to about 1 s; differences between frames are
    exact.
    """
    moi = cam.buffer_moi_index(0)  # the camera's own view of the split point
    print(f"Recorded {data.shape[0]} frames, configured pre_moi: {PRE_MOI} frames")
    print(f"MOI index reported by the camera: {moi}")
    print(f"Requested before the event: {PRE_MOI / FRAME_RATE * 1e3:.0f} ms")
    print(f"Requested after the event:  {(data.shape[0] - PRE_MOI) / FRAME_RATE * 1e3:.0f} ms")

    if headers and headers[0] is not None:
        print(f"First frame at {headers[0].datetime}")
        if 0 <= moi < len(headers) and headers[moi] is not None:
            print(f"MOI frame at {headers[moi].datetime}")
            print(f"  {headers[moi].timestamp - headers[0].timestamp:.4f} s into the recording")


def manual_flow(cam: Camera) -> tuple[np.ndarray, list[FrameHeader | None]]:
    """Arm, wait for the event, fire the MOI, wait for the recording."""
    cam.buffer_configure(
        n_sequences=1,
        frames_per_seq=FRAMES_PER_SEQ,
        pre_moi=PRE_MOI,
        moi_source="software",
    )

    cam.buffer_arm()  # the ring starts filling now
    input("Armed. Press Enter at the moment of interest...")
    cam.buffer_fire_moi()
    cam.buffer_wait(timeout=30.0)

    # return_headers gives the camera timestamp of every frame.
    return cam.buffer_download(sequence=0, return_headers=True)


def wait_for_flow(cam: Camera) -> tuple[np.ndarray, list[FrameHeader | None]]:
    """Same recording through buffer_record(wait_for=...).

    ``wait_for`` takes a delay in seconds or a callable that returns at the
    event. The callable runs once per sequence, after arming and before the
    MOI is fired.
    """
    cam.buffer_configure(
        n_sequences=1,
        frames_per_seq=FRAMES_PER_SEQ,
        pre_moi=PRE_MOI,
        moi_source="software",
    )

    cam.buffer_record(wait_for=lambda: input("Armed. Press Enter at the moment of interest..."))

    # return_headers gives the camera timestamp of every frame.
    return cam.buffer_download(sequence=0, return_headers=True)


def main() -> None:
    with Camera() as cam:
        cam.frame_rate = FRAME_RATE
        cam.integration_time_auto = "continuous"
        # Put the camera clock on host time. sync_time() writes whole seconds,
        # so absolute timestamps are good to about 1 s; frame-to-frame
        # differences are exact.
        cam.sync_time()

        print("Manual flow: arm, wait, fire, wait")
        data, headers = manual_flow(cam)
        report(cam, data, headers)
        np.save("pretrigger_manual.npy", data)
        cam.buffer_clear()

        print("\nSame with buffer_record(wait_for=...)")
        data, headers = wait_for_flow(cam)
        report(cam, data, headers)
        np.save("pretrigger_wait_for.npy", data)
        cam.buffer_clear()


if __name__ == "__main__":
    main()
