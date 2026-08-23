"""Pure converter helper tests (no FFmpeg / disk I/O needed)."""
from app.converter import _compute_target_size, _select_audio_by_language


def test_target_size_no_scaling_below_fhd():
    assert _compute_target_size(1920, 1080) is None
    assert _compute_target_size(1280, 720) is None
    assert _compute_target_size(0, 0) is None


def test_target_size_scales_to_fit():
    # 4K -> fits 1080p bounding box, even numbers
    w, h = _compute_target_size(3840, 2160)
    assert (w, h) == (1920, 1080)
    w2, h2 = _compute_target_size(2560, 1440)
    assert w2 <= 1920 and h2 <= 1080
    assert w2 % 2 == 0 and h2 % 2 == 0


def test_select_audio_prefers_matching_language():
    streams = [
        {"index": 0, "language": "eng", "title": "", "codec": "aac"},
        {"index": 1, "language": "tur", "title": "", "codec": "aac"},
        {"index": 2, "language": "tur", "title": "", "codec": "ac3"},
    ]
    idx, note = _select_audio_by_language(streams, ["tur", "tr", "trk"])
    assert idx == [1]  # AAC preferred among language matches


def test_select_audio_title_substring_fallback():
    streams = [
        {"index": 3, "language": "", "title": "Turkish 5.1", "codec": "aac"},
    ]
    idx, _ = _select_audio_by_language(streams, ["tur"])
    assert idx == [3]


def test_select_audio_falls_back_to_first_track():
    streams = [
        {"index": 5, "language": "jpn", "title": "", "codec": "flac"},
    ]
    idx, note = _select_audio_by_language(streams, ["tur"])
    assert idx == [5]
    assert "fallback" in note.lower()


def test_select_audio_no_streams():
    idx, note = _select_audio_by_language([], ["tur"])
    assert idx == []
