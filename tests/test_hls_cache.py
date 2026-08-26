import asyncio
import time
import pytest
from app.hls_cache import HlsCacheManager

def test_hls_cache_init():
    mgr = HlsCacheManager(max_size=50, max_bytes=1024 * 1024, m3u8_ttl=2.0)
    assert mgr.max_size == 50
    assert mgr.max_bytes == 1024 * 1024
    assert mgr.m3u8_ttl == 2.0
    assert mgr._total_bytes == 0

def test_hls_cache_byte_eviction():
    # 1 KB max memory
    mgr = HlsCacheManager(max_size=100, max_bytes=1000)
    data1 = b"x" * 600
    data2 = b"y" * 500

    mgr._cache["key1"] = (time.time(), data1, "video/mp2t")
    mgr._total_bytes += len(data1)

    # Adding data2 (500 bytes) will exceed 1000 bytes, so key1 should be evicted
    mgr._evict_if_needed(len(data2))
    assert "key1" not in mgr._cache
    assert mgr._total_bytes == 0

    mgr._cache["key2"] = (time.time(), data2, "video/mp2t")
    mgr._total_bytes += len(data2)
    assert mgr._total_bytes == 500

def test_hls_cache_count_eviction():
    # max_size 2
    mgr = HlsCacheManager(max_size=2, max_bytes=100000)
    mgr._cache["key1"] = (time.time(), b"1", "text/plain")
    mgr._cache["key2"] = (time.time(), b"2", "text/plain")
    mgr._total_bytes = 2

    # Third item triggers eviction of key1
    mgr._evict_if_needed(1)
    assert "key1" not in mgr._cache
    assert "key2" in mgr._cache
    assert mgr._total_bytes == 1

def test_active_viewer_counting():
    mgr = HlsCacheManager()
    port = 1935
    now = time.time()

    mgr._active_clients[port] = {
        "192.168.1.10": now - 2.0,   # Active (within 10s window)
        "192.168.1.11": now - 5.0,   # Active (within 10s window)
        "192.168.1.12": now - 15.0,  # Inactive (>10s window)
    }

    count = mgr.get_active_viewer_count(port, window_sec=10.0)
    assert count == 2

    # Port with no viewers
    assert mgr.get_active_viewer_count(9999) == 0
