"""PromptAssembler: builds the final, cache-optimized context string.

Layout (from position 0 so prefix caching hits as much as possible):

    [stable file 0 (most stable)]
    [stable file 1]
    ...
    [stable file N]
    [dirty file 0 — diff or full, depending on diff_threshold]
    [dirty file 1]
    ...

Key invariants:
- Canonical, deterministic file ordering (stability rank is deterministic for
  a given set of timestamps).
- Token budget is hard: least-stable files are dropped first; files are never
  silently truncated mid-content.
- Dirty file injection uses diff when diff_tokens < diff_threshold * file_tokens,
  full content otherwise.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import Config
from .estimator import CacheEstimator, count_tokens
from .ranker import StabilityRanker
from .state import FileStateManager

logger = logging.getLogger(__name__)


def _wrap(path: Path, content: str, tag: str = "file") -> str:
    """Wrap file content in a tagged block for clear model attribution."""
    return f'<{tag} path="{path}">\n{content}\n</{tag}>\n\n'


class PromptAssembler:
    def __init__(
        self,
        state: FileStateManager,
        ranker: StabilityRanker,
        estimator: CacheEstimator,
        config: Config,
    ) -> None:
        self._state = state
        self._ranker = ranker
        self._estimator = estimator
        self._config = config

    def assemble(self) -> tuple[str, dict]:
        """Build and return (context_string, metadata_dict).

        metadata includes token counts, file lists, and the cache estimate.
        """
        stable_paths = self._ranker.rank(self._state.get_stable_files())
        dirty_paths = self._state.get_dirty_files()

        remaining = self._config.token_budget
        stable_blocks: list[str] = []
        dropped_stable: list[str] = []

        # Stable files: most stable first, drop least stable when over budget
        for path in stable_paths:
            content = self._state.get_content(path)
            block = _wrap(path, content, tag="file")
            tokens = count_tokens(block)
            if tokens <= remaining:
                stable_blocks.append(block)
                remaining -= tokens
            else:
                dropped_stable.append(str(path))
                logger.debug("Budget: dropping stable file %s (%d tokens)", path, tokens)

        # Dirty files: decide diff vs full
        dirty_blocks: list[str] = []
        dropped_dirty: list[str] = []

        for path in dirty_paths:
            block, used_diff = self._dirty_block(path)
            tokens = count_tokens(block)
            if tokens <= remaining:
                dirty_blocks.append(block)
                remaining -= tokens
                logger.debug(
                    "Dirty %s: %s (%d tokens)", path, "diff" if used_diff else "full", tokens
                )
            else:
                dropped_dirty.append(str(path))
                logger.debug("Budget: dropping dirty file %s (%d tokens)", path, tokens)

        stable_content = "".join(stable_blocks)
        dirty_content = "".join(dirty_blocks)
        estimate = self._estimator.estimate(stable_content, dirty_content)

        metadata: dict = {
            "stable_file_count": len(stable_blocks),
            "dirty_file_count": len(dirty_blocks),
            "dropped_stable_files": dropped_stable,
            "dropped_dirty_files": dropped_dirty,
            **estimate.as_dict(),
        }

        return stable_content + dirty_content, metadata

    def assemble_stable_prefix(self) -> str:
        """Return only the stable prefix block (for the context://stable resource)."""
        stable_paths = self._ranker.rank(self._state.get_stable_files())
        remaining = self._config.token_budget
        blocks: list[str] = []
        for path in stable_paths:
            content = self._state.get_content(path)
            block = _wrap(path, content, tag="file")
            if count_tokens(block) <= remaining:
                blocks.append(block)
                remaining -= count_tokens(block)
        return "".join(blocks)

    def assemble_dirty_section(self) -> str:
        """Return only the dirty files section (for the context://dirty resource)."""
        blocks: list[str] = []
        for path in self._state.get_dirty_files():
            block, _ = self._dirty_block(path)
            blocks.append(block)
        return "".join(blocks)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dirty_block(self, path: Path) -> tuple[str, bool]:
        """Return (block_string, used_diff). Chooses diff vs full per heuristic."""
        content = self._state.get_content(path)
        diff = self._state.get_diff(path)

        if diff:
            file_tokens = count_tokens(content)
            diff_tokens = count_tokens(diff)
            if diff_tokens < self._config.diff_threshold * file_tokens:
                return _wrap(path, diff, tag="diff"), True

        return _wrap(path, content, tag="file"), False
