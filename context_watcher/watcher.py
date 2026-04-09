"""FileWatcher: wraps watchdog to feed filesystem events into FileStateManager.

Uses watchdog's Observer with a recursive watch on the configured root.
Events feed into FileStateManager (state) and StabilityRanker (ranker) so
everything stays in sync without the MCP server needing to poll.

The Observer runs in a daemon thread managed by watchdog; this module just
starts/stops it and routes events.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from .config import Config
from .ranker import StabilityRanker
from .state import FileStateManager

logger = logging.getLogger(__name__)


class _Handler(FileSystemEventHandler):
    def __init__(self, state: FileStateManager, ranker: StabilityRanker, config: Config) -> None:
        super().__init__()
        self._state = state
        self._ranker = ranker
        self._config = config

    def _is_ignored(self, src_path: str) -> bool:
        path = Path(src_path)
        for pattern in self._config.ignore_patterns:
            for part in path.parts:
                if fnmatch.fnmatch(part, pattern):
                    return True
            try:
                rel = path.relative_to(self._config.watch_root)
                if fnmatch.fnmatch(str(rel), pattern):
                    return True
            except ValueError:
                pass
        return False

    def on_modified(self, event: FileModifiedEvent) -> None:
        if event.is_directory or self._is_ignored(event.src_path):
            return
        path = Path(event.src_path)
        if self._state.update_file(path):
            self._ranker.record_change(path)
            logger.debug("Modified: %s", path)

    def on_created(self, event: FileCreatedEvent) -> None:
        if event.is_directory or self._is_ignored(event.src_path):
            return
        path = Path(event.src_path)
        self._state.index_file(path)
        # New file: treat as immediately dirty so agents see it
        self._state.update_file(path)
        self._ranker.record_change(path)
        logger.debug("Created: %s", path)

    def on_deleted(self, event: FileDeletedEvent) -> None:
        if event.is_directory or self._is_ignored(event.src_path):
            return
        path = Path(event.src_path)
        self._state.remove_file(path)
        self._ranker.remove(path)
        logger.debug("Deleted: %s", path)

    def on_moved(self, event: FileMovedEvent) -> None:
        if event.is_directory:
            return
        src = Path(event.src_path)
        dest = Path(event.dest_path)
        self._state.remove_file(src)
        self._ranker.remove(src)
        if not self._is_ignored(event.dest_path):
            self._state.index_file(dest)
            self._ranker.record_change(dest)
        logger.debug("Moved: %s → %s", src, dest)


class FileWatcher:
    def __init__(self, state: FileStateManager, ranker: StabilityRanker, config: Config) -> None:
        self._state = state
        self._ranker = ranker
        self._config = config
        self._observer: Observer = Observer()
        self._watch_handle = None

    def start(self) -> None:
        """Index current files then start the background observer thread."""
        root = self._config.watch_root
        logger.info("Indexing %s ...", root)
        count = self._state.index_directory(root)
        # Seed ranker for all already-stable files
        for path in self._state.get_stable_files():
            self._ranker.record_stable(path)
        logger.info("Indexed %d files in %s", count, root)

        handler = _Handler(self._state, self._ranker, self._config)
        self._watch_handle = self._observer.schedule(handler, str(root), recursive=True)
        self._observer.start()
        logger.info("Watching %s", root)

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()
        logger.info("Watcher stopped")

    def set_watch_root(self, new_root: Path) -> None:
        """Switch the watcher to a new directory without restarting the observer thread."""
        if self._watch_handle is not None:
            self._observer.unschedule(self._watch_handle)

        self._config.watch_root = new_root.resolve()

        # Re-index the new root
        logger.info("Re-indexing %s ...", new_root)
        count = self._state.index_directory(new_root)
        for path in self._state.get_stable_files():
            self._ranker.record_stable(path)
        logger.info("Indexed %d files in %s", count, new_root)

        handler = _Handler(self._state, self._ranker, self._config)
        self._watch_handle = self._observer.schedule(handler, str(new_root), recursive=True)
        logger.info("Now watching %s", new_root)
