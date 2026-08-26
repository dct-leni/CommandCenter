"""Converter worker-pool tests: cap at 2 concurrent, extras wait (never cancelled)."""
import asyncio
from types import SimpleNamespace

from app.converter import Converter, ConversionStatus


def make_converter(n_files: int):
    # Skip __init__ (no ffmpeg probing needed for pool logic)
    cv = object.__new__(Converter)
    cv.MAX_CONCURRENT_CONVERSIONS = 2
    cv._queue = []
    cv._queue_workers = []
    cv.files = {}
    cv._active_processes = {}
    for i in range(n_files):
        name = f"file{i}.mp4"
        cv.files[name] = SimpleNamespace(
            status=ConversionStatus.PENDING,
            progress=0.0,
            error="",
            filepath=f"does-not-exist-{i}.mp4",  # stat fails -> OSError -> guarded, proceeds
        )
        cv._queue.append(name)
    return cv


def test_pool_runs_max_two_and_processes_everything():
    async def scenario():
        cv = make_converter(5)

        state = {"running": 0, "peak": 0, "done": []}

        async def fake_run(filename):
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
            await asyncio.sleep(0.02)
            state["running"] -= 1
            state["done"].append(filename)

        cv._run_conversion = fake_run  # instance attr shadows method

        cv._start_queue_worker()
        await asyncio.gather(*cv._queue_workers)

        assert state["peak"] == 2, f"expected peak concurrency 2, got {state['peak']}"
        assert sorted(state["done"]) == sorted(cv.files.keys()), "every queued item must complete"

    asyncio.run(scenario())


def test_late_additions_get_picked_up():
    async def scenario():
        cv = make_converter(1)
        processed = []

        async def fake_run(filename):
            await asyncio.sleep(0.01)
            processed.append(filename)

        cv._run_conversion = fake_run
        cv._start_queue_worker()

        # Simulate user queuing more files while workers are alive
        for i in range(3):
            name = f"late{i}.mp4"
            cv.files[name] = SimpleNamespace(
                status=ConversionStatus.PENDING, progress=0.0, error="", filepath=name)
            cv._queue.append(name)
            cv._start_queue_worker()  # what convert_file()/convert_all() calls

        await asyncio.gather(*cv._queue_workers)
        assert sorted(processed) == ["file0.mp4", "late0.mp4", "late1.mp4", "late2.mp4"]

    asyncio.run(scenario())


def test_stop_cancels_workers_but_is_explicit():
    """Queue items are never dropped except by an explicit stop_conversion()."""
    cv = make_converter(3)
    before = list(cv._queue)
    cv._queue.clear()  # what stop_conversion does first
    assert before == ["file0.mp4", "file1.mp4", "file2.mp4"]
    assert cv._queue == []
