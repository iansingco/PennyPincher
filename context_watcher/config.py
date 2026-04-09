"""Configuration management for context-watcher.

Loads from `.context_watcher.toml` in the watch root or home directory.
All settings can also be overridden programmatically.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


# Files/directories to skip during initial indexing and watching
DEFAULT_IGNORE_PATTERNS: list[str] = [
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "*.pyc",
    "node_modules",
    ".venv",
    "venv",
    ".env",
    "*.egg-info",
    ".context_watcher_state.json",
    ".context_watcher.toml",
]


@dataclass
class Config:
    watch_root: Path = field(default_factory=lambda: Path(".").resolve())
    # Maximum tokens to include in assembled context
    token_budget: int = 100_000
    # Inject diff instead of full file when diff_tokens < diff_threshold * file_tokens
    diff_threshold: float = 0.20
    # Sidecar file for persisting stability scores
    state_file: str = ".context_watcher_state.json"
    # Optional log file (None = stdout)
    log_file: str | None = None
    # Glob patterns to ignore during indexing/watching
    ignore_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_PATTERNS))

    @classmethod
    def from_toml(cls, path: Path) -> "Config":
        with open(path, "rb") as f:
            data = tomllib.load(f)
        if "watch_root" in data:
            data["watch_root"] = Path(data["watch_root"]).resolve()
        if "ignore_patterns" in data:
            # Merge with defaults rather than replacing
            data["ignore_patterns"] = list(DEFAULT_IGNORE_PATTERNS) + data["ignore_patterns"]
        return cls(**data)

    @classmethod
    def load(cls, watch_root: Path | None = None) -> "Config":
        """Load config from .context_watcher.toml, searching watch_root then home."""
        search_dirs: list[Path] = []
        if watch_root:
            search_dirs.append(watch_root.resolve())
        search_dirs.append(Path.home())

        for d in search_dirs:
            candidate = d / ".context_watcher.toml"
            if candidate.exists():
                cfg = cls.from_toml(candidate)
                if watch_root:
                    cfg.watch_root = watch_root.resolve()
                return cfg

        cfg = cls()
        if watch_root:
            cfg.watch_root = watch_root.resolve()
        return cfg

    @property
    def state_file_path(self) -> Path:
        return self.watch_root / self.state_file
