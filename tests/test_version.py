import pytest
from starlette.testclient import TestClient
from app.__version__ import __version__
from app.main import app


def test_version_string():
    assert isinstance(__version__, str)
    assert len(__version__) > 0
    parts = __version__.split(".")
    assert len(parts) >= 2


def test_fastapi_version_matches():
    assert app.version == __version__


def test_root_html_contains_dynamic_version():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert f'id="app-version-badge">v{__version__}</span>' in response.text
    assert "V0.3" not in response.text


def test_system_status_api_contains_version():
    client = TestClient(app)
    response = client.get("/api/system/status")
    assert response.status_code == 200
    data = response.json()
    assert data["version"] == __version__
