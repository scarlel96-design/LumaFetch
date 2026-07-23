"""Thread and UI-event lifecycle primitives used by the desktop application."""

from __future__ import annotations

import queue
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any


class UiEventBus:
    """A bounded, non-blocking event channel for background workers.

    Producers never wait for Tk. High-volume preview/progress events may be
    dropped under pressure, while terminal/error events evict older disposable
    entries so worker shutdown cannot deadlock on a full queue.
    """

    _DROPPABLE = {
        "log",
        "progress",
        "preview_cover_item",
        "live_preview_item",
        "update_download_progress",
    }
    _TERMINAL = {
        "done",
        "error",
        "preview_cover_error",
        "live_preview_done",
        "live_preview_error",
        "preview_session_finished",
        "viewer_image",
        "viewer_error",
        "update_result",
        "update_error",
        "update_download_done",
        "update_download_error",
    }

    def __init__(self, maxsize: int = 2048) -> None:
        self._queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=maxsize)
        self._closed = threading.Event()
        self._dropped = 0

    def publish(self, kind: str, payload: object) -> bool:
        if self._closed.is_set():
            return False
        try:
            self._queue.put_nowait((kind, payload))
            return True
        except queue.Full:
            if kind in self._DROPPABLE:
                self._dropped += 1
                return False
            # Preserve completion/error signals by discarding a bounded number
            # of older UI-only events. No producer is ever allowed to block.
            for _ in range(64 if kind in self._TERMINAL else 8):
                try:
                    old_kind, old_payload = self._queue.get_nowait()
                except queue.Empty:
                    break
                if old_kind not in self._DROPPABLE:
                    try:
                        self._queue.put_nowait((old_kind, old_payload))
                    except queue.Full:
                        pass
                try:
                    self._queue.put_nowait((kind, payload))
                    return True
                except queue.Full:
                    continue
            self._dropped += 1
            return False

    def put(self, item: tuple[str, object]) -> bool:
        kind, payload = item
        return self.publish(kind, payload)

    def discard_stale_preview_events(self, current_sequence: int) -> int:
        preview_kinds = {
            "preview_cover_item", "preview_cover_error", "live_preview_start",
            "live_preview_item", "live_preview_done", "live_preview_error",
        }
        removed = 0
        with self._queue.mutex:
            retained = []
            for kind, payload in self._queue.queue:
                sequence = payload[0] if kind in preview_kinds and isinstance(payload, tuple) and payload else None
                if kind in preview_kinds and sequence != current_sequence:
                    removed += 1
                else:
                    retained.append((kind, payload))
            self._queue.queue.clear()
            self._queue.queue.extend(retained)
            self._queue.not_full.notify_all()
        return removed

    def get_nowait(self) -> tuple[str, object]:
        return self._queue.get_nowait()

    def empty(self) -> bool:
        return self._queue.empty()

    def qsize(self) -> int:
        return self._queue.qsize()

    @property
    def dropped(self) -> int:
        return self._dropped

    def close(self) -> None:
        self._closed.set()


class WorkerRegistry:
    """Track finite background threads and release references when they finish."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: set[threading.Thread] = set()
        self._closing = False

    def start(
        self,
        target: Callable[..., Any],
        *,
        args: tuple[Any, ...] = (),
        name: str,
    ) -> threading.Thread | None:
        with self._lock:
            if self._closing:
                return None

        def runner() -> None:
            try:
                target(*args)
            finally:
                with self._lock:
                    self._threads.discard(threading.current_thread())

        thread = threading.Thread(target=runner, name=name, daemon=True)
        with self._lock:
            if self._closing:
                return None
            self._threads.add(thread)
        thread.start()
        return thread

    def close_and_join(self, timeout: float = 2.5) -> int:
        with self._lock:
            self._closing = True
            threads = list(self._threads)
        deadline = __import__("time").monotonic() + max(0.0, timeout)
        for thread in threads:
            remaining = deadline - __import__("time").monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)
        with self._lock:
            return sum(thread.is_alive() for thread in self._threads)

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(thread.is_alive() for thread in self._threads)


class PreviewCacheManager:
    """Coordinate unique preview sessions and delete only completed retired caches."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._finished: set[Path] = set()
        self._retired: list[Path] = []
        self._protected: set[Path] = set()

    def create(self, sequence: int) -> Path:
        path = self.root / f"session-{sequence:08d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def retire(self, path: Path | None) -> list[Path]:
        if path is None:
            return []
        with self._lock:
            if path not in self._retired:
                self._retired.append(path)
            return self._collect_ready_locked()

    def finish(self, path: Path) -> list[Path]:
        with self._lock:
            self._finished.add(path)
            return self._collect_ready_locked()

    def protect(self, path: Path | None) -> None:
        if path is None:
            return
        with self._lock:
            self._protected.add(path)

    def unprotect(self, path: Path | None) -> list[Path]:
        if path is None:
            return []
        with self._lock:
            self._protected.discard(path)
            return self._collect_ready_locked()

    def _collect_ready_locked(self) -> list[Path]:
        # Keep the newest retired cache as a short-lived safety buffer for Tk
        # callbacks and viewer decode work that may already have been queued.
        safe_candidates = self._retired[:-1]
        ready = [
            path for path in safe_candidates
            if path in self._finished and path not in self._protected
        ]
        if ready:
            ready_set = set(ready)
            self._retired = [path for path in self._retired if path not in ready_set]
            self._finished.difference_update(ready_set)
        return ready

    @staticmethod
    def remove(path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
