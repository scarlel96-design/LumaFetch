from __future__ import annotations

import threading
import time
from pathlib import Path

from lumafetch.runtime import PreviewCacheManager, UiEventBus, WorkerRegistry


def test_event_bus_never_blocks_when_preview_events_flood_queue() -> None:
    bus = UiEventBus(maxsize=8)
    started = time.monotonic()
    for index in range(50_000):
        bus.put(("live_preview_item", (1, index, object())))
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert bus.qsize() <= 8
    assert bus.dropped > 0


def test_terminal_event_survives_full_droppable_queue() -> None:
    bus = UiEventBus(maxsize=4)
    for index in range(20):
        bus.put(("progress", index))
    assert bus.put(("done", "complete"))
    drained = []
    while not bus.empty():
        drained.append(bus.get_nowait())
    assert ("done", "complete") in drained


def test_stale_preview_events_can_be_purged_by_sequence() -> None:
    bus = UiEventBus(maxsize=32)
    bus.put(("live_preview_item", (1, 0, object())))
    bus.put(("live_preview_done", (1, object())))
    bus.put(("live_preview_item", (2, 0, object())))
    bus.put(("log", "keep"))
    assert bus.discard_stale_preview_events(2) == 2
    remaining = []
    while not bus.empty():
        remaining.append(bus.get_nowait())
    assert any(kind == "live_preview_item" and payload[0] == 2 for kind, payload in remaining)
    assert ("log", "keep") in remaining


def test_worker_registry_releases_finished_threads() -> None:
    registry = WorkerRegistry()
    completed = threading.Event()
    registry.start(completed.set, name="unit-worker")
    assert completed.wait(1.0)
    for _ in range(50):
        if registry.active_count == 0:
            break
        time.sleep(0.01)
    assert registry.active_count == 0
    assert registry.close_and_join(timeout=0.2) == 0


def test_preview_cache_waits_for_finish_and_protection(tmp_path: Path) -> None:
    manager = PreviewCacheManager(tmp_path)
    first = manager.create(1)
    second = manager.create(2)
    manager.protect(first)
    assert manager.retire(first) == []
    assert manager.finish(first) == []
    assert manager.retire(second) == []
    assert manager.unprotect(first) == [first]
    manager.remove(first)
    assert not first.exists()
    manager.close()
