"""FileStateManager: tracks file content hashes, dirty flags, and diffs.

Each file is either:
- stable: last-known content matches what's on disk (or was manually marked stable)
- dirty:  on-disk content has changed since last stable snapshot

Diffs are computed lazily via difflib.unified_diff — no external dependency.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class FileState:
    path: Path
    content_hash: str
    # Content at the time the file was last marked clean / first indexed
    clean_content: str
    # Current on-disk content when dirty; None when clean
    dirty_content: str | None = None

    @property
    def is_dirty(self) -> bool:
        return self.dirty_content is not None

    def compute_diff(self) -> str:
        """Return unified diff of clean→dirty. Empty string if not dirty."""
        if not self.is_dirty:
            return ""
        from_lines = self.clean_content.splitlines(keepends=True)
        to_lines = self.dirty_content.splitlines(keepends=True)  # type: ignore[union-attr]
        return "".join(
            difflib.unified_diff(
                from_lines,
                to_lines,
                fromfile=f"a/{self.path}",
                tofile=f"b/{self.path}",
            )
        )

    @property
    def current_content(self) -> str:
        return self.dirty_content if self.is_dirty else self.clean_content


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode(errors="replace")).hexdigest()


class FileStateManager:
    """Thread-safe-ish registry of file states.

    The watcher callback and MCP tool handlers run in different threads/tasks,
    so all mutations go through simple dict operations (GIL-protected in CPython).
    For a production deployment you'd add an asyncio.Lock; for v1 CPython is fine.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._states: dict[str, FileState] = {}

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def index_directory(self, root: Path) -> int:
        """Walk root recursively, index every non-ignored file. Returns file count."""
        count = 0
        for path in root.rglob("*"):
            if path.is_file() and not self._is_ignored(path):
                try:
                    self.index_file(path)
                    count += 1
                except (OSError, UnicodeDecodeError) as exc:
                    logger.debug("Skipping %s: %s", path, exc)
        return count

    def index_file(self, path: Path) -> None:
        """Add or refresh a file into the stable index."""
        content = path.read_text(errors="replace")
        self._states[str(path)] = FileState(
            path=path,
            content_hash=_hash(content),
            clean_content=content,
            dirty_content=None,
        )

    # ------------------------------------------------------------------
    # Mutation (called by watcher)
    # ------------------------------------------------------------------

    def update_file(self, path: Path) -> bool:
        """Called when the filesystem watcher detects a change.

        Returns True if the content actually changed (i.e. not a spurious event).
        """
        try:
            new_content = path.read_text(errors="replace")
        except OSError as exc:
            logger.warning("Could not read %s: %s", path, exc)
            return False

        new_hash = _hash(new_content)
        key = str(path)

        if key not in self._states:
            # New file observed for the first time — index it as dirty immediately
            self._states[key] = FileState(
                path=path,
                content_hash=new_hash,
                clean_content="",
                dirty_content=new_content,
            )
            return True

        state = self._states[key]
        if new_hash == state.content_hash:
            return False  # Spurious event, nothing changed

        state.dirty_content = new_content
        state.content_hash = new_hash
        return True

    def remove_file(self, path: Path) -> None:
        self._states.pop(str(path), None)

    def mark_stable(self, path: Path) -> bool:
        """Promote current content to clean baseline. Returns False if unknown path."""
        state = self._states.get(str(path))
        if state is None:
            return False
        if state.is_dirty:
            state.clean_content = state.dirty_content  # type: ignore[assignment]
            state.dirty_content = None
        return True

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_dirty_files(self) -> list[Path]:
        return [s.path for s in self._states.values() if s.is_dirty]

    def get_stable_files(self) -> list[Path]:
        return [s.path for s in self._states.values() if not s.is_dirty]

    def get_all_files(self) -> list[Path]:
        return [s.path for s in self._states.values()]

    def get_content(self, path: Path) -> str:
        state = self._states.get(str(path))
        return state.current_content if state else ""

    def get_diff(self, path: Path) -> str:
        state = self._states.get(str(path))
        return state.compute_diff() if state else ""

    def is_known(self, path: Path) -> bool:
        return str(path) in self._states

    def as_json(self) -> dict:
        return {
            key: {
                "is_dirty": s.is_dirty,
                "content_hash": s.content_hash,
            }
            for key, s in self._states.items()
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_ignored(self, path: Path) -> bool:
        for pattern in self._config.ignore_patterns:
            # Match against each path component and the full relative path
            rel = path.relative_to(self._config.watch_root) if path.is_absolute() else path
            for part in rel.parts:
                if fnmatch.fnmatch(part, pattern):
                    return True
            if fnmatch.fnmatch(str(rel), pattern):
                return True
        return False
