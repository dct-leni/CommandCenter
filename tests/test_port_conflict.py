"""Port-conflict validation helper tests (pure logic, no I/O)."""
from types import SimpleNamespace

import pytest

from app.routers.common import check_port_conflict, check_port_range_conflict


def make_cfg(ranges=(1935, 1944), slots=None, live=None):
    return SimpleNamespace(streamer=SimpleNamespace(
        port_range_start=ranges[0],
        port_range_end=ranges[1],
        playlists=slots or {},
        live_streams=live or [],
    ))


def test_port_inside_folder_range_rejected():
    with pytest.raises(Exception, match="folder stream port range"):
        check_port_conflict(1940, make_cfg())


def test_port_assigned_to_slot_rejected():
    cfg = make_cfg(slots={"0101_0201": [{"port": 2000}]})
    with pytest.raises(Exception, match="slot"):
        check_port_conflict(2000, cfg)


def test_port_used_by_live_stream_rejected():
    cfg = make_cfg(live=[{"id": "a", "name": "X", "port": 2000}])
    with pytest.raises(Exception, match="live stream 'X'"):
        check_port_conflict(2000, cfg)


def test_ignore_live_stream_id_allows_same_port():
    cfg = make_cfg(live=[{"id": "a", "name": "X", "port": 2000}])
    check_port_conflict(2000, cfg, ignore_live_stream_id="a")  # no raise


def test_free_port_passes():
    check_port_conflict(2000, make_cfg())  # no raise


def test_inverted_range_rejected():
    with pytest.raises(Exception, match="greater than end"):
        check_port_range_conflict(3000, 2999, make_cfg())


def test_range_crossing_live_stream_rejected():
    cfg = make_cfg(live=[{"id": "a", "name": "X", "port": 2500}])
    with pytest.raises(Exception, match="2500"):
        check_port_range_conflict(2400, 2600, cfg)


def test_valid_range_passes():
    check_port_range_conflict(2000, 2010, make_cfg())  # no raise
