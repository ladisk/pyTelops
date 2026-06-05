from pyTelops.camera import _build_integrity_report
from pyTelops.errors import DownloadStats, FrameIntegrityError


def test_download_stats_defaults():
    s = DownloadStats(n_frames=10)
    assert s.n_frames == 10
    assert s.n_incomplete == 0
    assert s.incomplete_frame_ids == []
    assert s.per_frame_missing == {}
    assert s.resend_requested == 0


def test_frame_integrity_error_carries_stats():
    s = DownloadStats(n_frames=10, n_incomplete=2, incomplete_frame_ids=[3, 7])
    err = FrameIntegrityError("boom", stats=s)
    assert err.stats is s
    assert "boom" in str(err)


def test_build_integrity_report_all_perfect():
    info = [(i, {"block_id": 100 + i, "missing_packets": 0}) for i in range(5)]
    stats = _build_integrity_report(
        per_frame_info=info,
        resend_stats={"requested": 0, "recovered": 0, "failed": 0},
        n_requested=5,
    )
    assert stats.n_frames == 5
    assert stats.n_incomplete == 0
    assert stats.incomplete_frame_ids == []
    assert stats.per_frame_missing == {}


def test_build_integrity_report_flags_incomplete():
    info = [
        (0, {"block_id": 100, "missing_packets": 0}),
        (1, {"block_id": 101, "missing_packets": 4}),
        (2, {"block_id": 102, "missing_packets": 0}),
        (3, {"block_id": 103, "missing_packets": 1}),
    ]
    stats = _build_integrity_report(
        per_frame_info=info,
        resend_stats={"requested": 12, "recovered": 7, "failed": 5},
        n_requested=5,
    )
    assert stats.n_frames == 4
    assert sorted(stats.incomplete_frame_ids) == [101, 103]
    assert stats.per_frame_missing == {101: 4, 103: 1}
    # 4 frames arrived, 5 requested -> 1 never arrived. n_incomplete counts
    # both arrived-but-partial (2) AND never-arrived (1) = 3; incomplete_frame_ids
    # lists only the retryable arrived-partial subset.
    assert stats.n_incomplete == 3
    assert stats.resend_requested == 12
    assert stats.resend_recovered == 7
    assert stats.resend_failed == 5
