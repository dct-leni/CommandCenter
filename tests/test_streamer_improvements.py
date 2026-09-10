import pytest
from datetime import date
from app.streamer import Streamer, PortSlot, DateRangeFolder

def test_ffconcat_path_escaping(tmp_path):
    mgr = Streamer()
    mgr.config_path = str(tmp_path / "config.json")
    
    # Path with apostrophes and spaces
    file_with_quote = "Director's Cut - O'Connor.ts"
    full_path = str(tmp_path / file_with_quote)
    
    slot = PortSlot(
        port=1935,
        files=[file_with_quote],
        paths=[full_path],
        durations=[120.0]
    )
    folder = DateRangeFolder(
        name="0101_0501",
        path=str(tmp_path),
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 5),
        files=[file_with_quote]
    )
    
    playlist_path = mgr._build_ffmpeg_concat_playlist(slot, 0, 0.0, folder)
    with open(playlist_path, "r", encoding="utf-8") as f:
        content = f.read()
        
    # Check that ffconcat header is present
    assert "ffconcat version 1.0" in content
    # Check that apostrophes are escaped with '\''
    assert "Director'\\''s Cut - O'\\''Connor.ts" in content

def test_compute_slot_seek_zero_durations():
    mgr = Streamer()
    slot = PortSlot(
        port=1935,
        files=["test1.ts", "test2.ts"],
        paths=["/dummy/test1.ts", "/dummy/test2.ts"],
        durations=[0.0, 0.0]  # unprobed / zero duration
    )
    # Should safely return 0, 0.0 without division by zero
    idx, offset = mgr._compute_slot_seek(slot, date(2026, 1, 1))
    assert idx == 0
    assert offset == 0.0
