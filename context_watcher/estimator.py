"""CacheEstimator: token counting and prefix cache hit rate tracking.

Uses tiktoken (cl100k_base) for accurate counting when available.
Falls back to a character-based approximation (~4 chars/token) if not installed.

The estimator doesn't know the provider's actual cache state — it estimates
by assuming all tokens in the stable prefix are cached (a best-case bound).
Real savings will vary based on provider TTL and session continuity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

try:
    import tiktoken

    _enc = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_enc.encode(text, disallowed_special=()))

    logger.debug("tiktoken loaded; using cl100k_base encoder")

except Exception:
    # ImportError if tiktoken isn't installed; OSError/requests errors if the BPE
    # data can't be fetched (e.g. no network access in sandboxed environments).
    logger.warning(
        "tiktoken encoder unavailable; using character-based approximation (~4 chars/token)"
    )

    def count_tokens(text: str) -> int:  # type: ignore[misc]
        return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Estimator
# ---------------------------------------------------------------------------


@dataclass
class CacheEstimate:
    stable_tokens: int
    dirty_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.stable_tokens + self.dirty_tokens

    @property
    def cache_hit_rate(self) -> float:
        if self.total_tokens == 0:
            return 0.0
        return self.stable_tokens / self.total_tokens

    def as_dict(self) -> dict:
        return {
            "total_tokens": self.total_tokens,
            "estimated_cached_tokens": self.stable_tokens,
            "estimated_uncached_tokens": self.dirty_tokens,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
        }


class CacheEstimator:
    def __init__(self) -> None:
        self._last: CacheEstimate | None = None
        self._session_total = 0
        self._session_cached = 0

    def estimate(self, stable_content: str, dirty_content: str) -> CacheEstimate:
        est = CacheEstimate(
            stable_tokens=count_tokens(stable_content),
            dirty_tokens=count_tokens(dirty_content),
        )
        self._last = est
        self._session_total += est.total_tokens
        self._session_cached += est.stable_tokens
        logger.info(
            "Cache estimate: %d/%d tokens cached (%.1f%%) | session total: %d/%d (%.1f%%)",
            est.stable_tokens,
            est.total_tokens,
            est.cache_hit_rate * 100,
            self._session_cached,
            self._session_total,
            (self._session_cached / self._session_total * 100) if self._session_total else 0,
        )
        return est

    def get_last_estimate(self) -> CacheEstimate | None:
        return self._last

    def session_stats(self) -> dict:
        rate = self._session_cached / self._session_total if self._session_total else 0.0
        return {
            "session_total_tokens": self._session_total,
            "session_cached_tokens": self._session_cached,
            "session_cache_hit_rate": round(rate, 4),
        }
