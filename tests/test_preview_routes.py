from datetime import date
import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
from app.converter import converter
from app.streamer import streamer, DateRangeFolder

@pytest.fixture
def client():
    return TestClient(app)

def test_converter_stream_file(tmp_path, client):
    folder = tmp_path / "conv_test"
    folder.mkdir()
    sample_file = folder / "video1.ts"
    sample_file.write_bytes(b"\x47" * 188) # MPEG-TS sync byte sample

    converter.source_folder = str(folder)
    converter.files = {}

    # Valid file request
    resp = client.get("/api/converter/file/video1.ts")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/mp2t"
    assert resp.content == b"\x47" * 188

    # Non-existent file
    resp_404 = client.get("/api/converter/file/nonexistent.ts")
    assert resp_404.status_code == 404

    # Path traversal attempt
    resp_traversal = client.get("/api/converter/file/..%2F..%2Fconfig.yml")
    assert resp_traversal.status_code in (400, 404)

def test_streamer_stream_file(tmp_path, client):
    folder = tmp_path / "stream_test"
    folder.mkdir()
    sample_file = folder / "stream1.ts"
    sample_file.write_bytes(b"\x47" * 188)

    sf = DateRangeFolder(
        name="TestFolder",
        path=str(folder),
        start_date=date(2026, 8, 27),
        end_date=date(2026, 8, 28),
        files=["stream1.ts"]
    )
    streamer.folders = [sf]

    # Valid file request
    resp = client.get("/api/streamer/folder/TestFolder/file/stream1.ts")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/mp2t"
    assert resp.content == b"\x47" * 188

    # Non-existent folder
    resp_no_folder = client.get("/api/streamer/folder/NonExistent/file/stream1.ts")
    assert resp_no_folder.status_code == 404

    # Non-existent file
    resp_no_file = client.get("/api/streamer/folder/TestFolder/file/nofile.ts")
    assert resp_no_file.status_code == 404
