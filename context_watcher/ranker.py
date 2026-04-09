"""StabilityRanker: orders files by how long ago they last changed.

Stability score = seconds since last recorded change.
Higher score = more stable = should appear earlier in the context prefix.

Scores persist to a JSON sidecar between sessions so the watcher doesn't
start cold after a restart.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class FileScore:
    path: str
    # Unix timestamp of the last observed change. 0 = manually marked stable / never changed.
    last_changed: float

    @property
    def stability_score(self) -> float:
        """Higher = more stable. Never-changed files get very high implicit score."""
        if self.last_changed == 0:
            return float("inf")
        return time.time() - self.last_changed


class StabilityRanker:
    def __init__(self, state_file: Path) -> None:
        self._state_file = state_file
        self._scores: dict[str, FileScore] = {}
        self._load()

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def record_change(self, path: Path) -> None:
        """Called when a file is modified on disk."""
        self._scores[str(path)] = FileScore(path=str(path), last_changed=time.time())
        self._save()

    def record_stable(self, path: Path) -> None:
        """Ensure the file has a score entry; don't update timestamp."""
        key = str(path)
        if key not in self._scores:
            self._scores[key] = FileScore(path=key, last_changed=0)

    def mark_stable(self, path: Path) -> None:
        """Manually pin a file to the top of the stable prefix (last_changed=0)."""
        self._scores[str(path)] = FileScore(path=str(path), last_changed=0)
        self._save()

    def remove(self, path: Path) -> None:
        self._scores.pop(str(path), None)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def rank(self, paths: list[Path]) -> list[Path]:
        """Return paths sorted most-stable → least-stable."""
        def score(p: Path) -> float:
            s = self._scores.get(str(p))
            # Unknown files (not yet scored) are treated as maximally stable
            return s.stability_score if s else float("inf")

        return sorted(paths, key=score, reverse=True)

    def get_score(self, path: Path) -> float | None:
        s = self._scores.get(str(path))
        return s.stability_score if s else None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._state_file.exists():
            return
        try:
            with open(self._state_file) as f:
                data: dict = json.load(f)
            for key, val in data.items():
                self._scores[key] = FileScore(**val)
            logger.debug("Loaded %d stability scores from %s", len(self._scores), self._state_file)
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            logger.warning("Could not load stability scores: %s", exc)

    def _save(self) -> None:
        try:
            with open(self._state_file, "w") as f:
                json.dump({k: asdict(v) for k, v in self._scores.items()}, f, indent=2)
        except OSError as exc:
            logger.warning("Could not save stability scores: %s", exc)
