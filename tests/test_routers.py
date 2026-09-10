import pytest
from starlette.testclient import TestClient
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_system_status_endpoint(client):
    response = client.get("/api/system/status")
    assert response.status_code == 200
    data = response.json()
    assert "ffmpeg" in data
    assert "mediamtx" in data
    assert "best_encoder" in data


def test_config_get_and_put(client):
    # GET config
    response = client.get("/api/config")
    assert response.status_code == 200
    cfg = response.json()
    assert "streamer" in cfg
    assert "shader_upscale" in cfg["streamer"]
    assert "video_capture_backend" in cfg["streamer"]

    # Toggle shader_upscale via PUT
    current_val = cfg["streamer"]["shader_upscale"]
    update_payload = {
        "streamer": {
            "shader_upscale": not current_val
        }
    }
    put_res = client.put("/api/config", json=update_payload)
    assert put_res.status_code == 200
    updated_cfg = put_res.json()
    assert updated_cfg["streamer"]["shader_upscale"] == (not current_val)

    # Revert
    client.put("/api/config", json={"streamer": {"shader_upscale": current_val}})


def test_converter_status_endpoint(client):
    response = client.get("/api/converter/status")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)
    assert "files" in data


def test_live_streams_list_endpoint(client):
    response = client.get("/api/streamer/live_streams")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)
    assert "live_streams" in data
    assert isinstance(data["live_streams"], list)
