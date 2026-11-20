"""Focus assist helpers of the live viewer (no display needed)."""

import numpy as np

from pyTelops.gui import focus_box, peaking_mask, sharpness


def _edge(blur: int) -> np.ndarray:
    """A vertical step edge, softened by a box blur of *blur* pixels."""
    img = np.zeros((32, 64), np.float32)
    img[:, 32:] = 10.0
    if blur > 1:
        k = np.ones(blur) / blur
        pad = blur // 2
        img = np.apply_along_axis(
            lambda r: np.convolve(np.pad(r, pad, mode="edge"), k, mode="valid")[: r.size], 1, img
        )
    return img


def test_sharp_edge_scores_higher_than_blurred_edge():
    assert sharpness(_edge(1)) > sharpness(_edge(5)) > sharpness(_edge(11))


def test_sharpness_box_limits_the_region():
    img = _edge(1)
    assert sharpness(img, (0, 0, 16, 32)) == 0.0  # flat part only
    assert sharpness(img, (24, 0, 40, 32)) > 0.0  # contains the edge


def test_sharpness_of_tiny_region_is_zero():
    assert sharpness(np.ones((4, 4))) == 0.0


def test_peaking_marks_the_edge_only():
    mask = peaking_mask(_edge(1))
    assert mask.any()
    cols = np.unique(np.nonzero(mask)[1])
    assert set(cols) <= {30, 31, 32, 33}


def test_peaking_marks_nothing_on_a_flat_image():
    assert not peaking_mask(np.full((32, 32), 25.0)).any()


def test_peaking_ignores_uniform_noise():
    rng = np.random.default_rng(0)
    noise = rng.normal(25.0, 0.05, (64, 64))
    assert peaking_mask(noise).mean() < 0.01


def test_focus_box_is_centred_and_kept_inside_the_frame():
    assert focus_box(160, 128, 320, 256, 64) == (128, 96, 192, 160)
    assert focus_box(0, 0, 320, 256, 64) == (0, 0, 64, 64)
    assert focus_box(319, 255, 320, 256, 64) == (256, 192, 320, 256)
    assert focus_box(10, 2, 64, 4, 64) == (0, 0, 64, 4)  # frame smaller than the box


def test_sharpness_ranks_focus_through_sensor_noise():
    # Low-contrast scene (1 degree step) with pixel noise, as on the real camera.
    rng = np.random.default_rng(1)
    noise = rng.normal(0.0, 0.15, (32, 64)).astype(np.float32)
    sharp = sharpness(_edge(1) / 10 + noise)
    blurred = sharpness(_edge(9) / 10 + noise)
    assert sharp > 1.5 * blurred


def test_peaking_draws_a_thin_line_on_a_soft_edge():
    mask = peaking_mask(_edge(5))
    per_row = mask.sum(axis=1)
    assert per_row.max() <= 2
    assert per_row[5:-5].min() >= 1


def test_sharpness_does_not_change_with_contrast():
    # A cooling specimen lowers the contrast; the score must stay the same.
    rng = np.random.default_rng(2)
    noise = rng.normal(0.0, 0.05, (32, 64)).astype(np.float32)
    hot = sharpness(_edge(3) + noise)
    cooled = sharpness(0.3 * (_edge(3) + noise) + 20.0)
    assert abs(hot - cooled) / hot < 0.01
