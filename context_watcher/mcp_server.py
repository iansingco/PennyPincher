"""MCP server for context-watcher.

Exposes the following tools:
  get_context()          → Fully assembled, cache-optimized context block
  get_dirty_files()      → Files changed since last request
  get_diff(path)         → Unified diff for a specific dirty file
  get_cache_estimate()   → Estimated cached vs uncached tokens for last request
  mark_stable(path)      → Manually promote a file to the stable prefix
  set_watch_root(path)   → Point watcher at a different directory

And the following resources:
  context://stable       → Current stable prefix block
  context://dirty        → Current dirty files with diffs
  context://state        → Full watcher state as JSON

Run with:
  python -m context_watcher.mcp_server [--watch-root PATH] [--token-budget N]

Or via the installed entry point:
  context-watcher [--watch-root PATH]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .assembler import PromptAssembler
from .config import Config
from .estimator import CacheEstimator
from .ranker import StabilityRanker
from .state import FileStateManager
from .watcher import FileWatcher

logger = logging.getLogger(__name__)


def _build_server(config: Config) -> FastMCP:
    # ---------------------------------------------------------------------------
    # Component wiring
    # ---------------------------------------------------------------------------
    state = FileStateManager(config)
    ranker = StabilityRanker(config.state_file_path)
    estimator = CacheEstimator()
    assembler = PromptAssembler(state, ranker, estimator, config)
    fw = FileWatcher(state, ranker, config)

    # ---------------------------------------------------------------------------
    # Lifecycle: start watcher on server startup, stop on shutdown
    # ---------------------------------------------------------------------------
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(server: FastMCP):  # type: ignore[type-arg]
        fw.start()
        try:
            yield {}
        finally:
            fw.stop()

    mcp = FastMCP(
        "context-watcher",
        instructions=(
            "A filesystem-watching, cache-aware context assembly service. "
            "Call get_context() to receive optimally ordered file context that maximizes "
            "prefix cache hit rates. Call get_dirty_files() first to decide whether "
            "a fresh context fetch is needed."
        ),
        lifespan=_lifespan,
    )

    # ---------------------------------------------------------------------------
    # Tools
    # ---------------------------------------------------------------------------

    @mcp.tool(description=(
        "Return a fully assembled, cache-optimized context block. "
        "Stable files (ranked most-stable first) appear before dirty files, "
        "maximizing the provider's prefix cache hit rate. "
        "Dirty files are injected as unified diffs when the diff is under "
        f"{int(config.diff_threshold * 100)}% of the file's token count, "
        "and as full content otherwise. "
        "Token budget is enforced; least-stable files are dropped first."
    ))
    def get_context() -> dict:
        context, metadata = assembler.assemble()
        return {"context": context, "metadata": metadata}

    @mcp.tool(description=(
        "Return the list of files that have changed since they were last indexed or marked stable. "
        "Use this to decide whether calling get_context() is necessary."
    ))
    def get_dirty_files() -> dict:
        dirty = [str(p) for p in state.get_dirty_files()]
        return {"dirty_files": dirty, "count": len(dirty)}

    @mcp.tool(description=(
        "Return the unified diff for a specific dirty file. "
        "The path must be absolute or relative to the watch root."
    ))
    def get_diff(path: str) -> dict:
        p = _resolve_path(path, config.watch_root)
        diff = state.get_diff(p)
        if not diff:
            if not state.is_known(p):
                return {"error": f"Unknown file: {path}", "diff": ""}
            return {"diff": "", "note": "File is clean (not dirty)"}
        return {"path": str(p), "diff": diff}

    @mcp.tool(description=(
        "Return the estimated token counts for the last get_context() call, "
        "broken down into cached (stable prefix) and uncached (dirty) tokens. "
        "Also includes session-level totals."
    ))
    def get_cache_estimate() -> dict:
        last = estimator.get_last_estimate()
        if last is None:
            return {"note": "No context assembled yet this session.", **estimator.session_stats()}
        return {**last.as_dict(), **estimator.session_stats()}

    @mcp.tool(description=(
        "Manually promote a file to the stable prefix by resetting its stability score "
        "to the maximum. Useful for files the agent knows won't change this session."
    ))
    def mark_stable(path: str) -> dict:
        p = _resolve_path(path, config.watch_root)
        if not state.mark_stable(p):
            return {"error": f"Unknown file: {path}"}
        ranker.mark_stable(p)
        return {"status": "ok", "path": str(p)}

    @mcp.tool(description=(
        "Point the watcher at a different directory. "
        "The previous watch root is unregistered; the new root is indexed immediately."
    ))
    def set_watch_root(path: str) -> dict:
        new_root = Path(path).resolve()
        if not new_root.is_dir():
            return {"error": f"Not a directory: {path}"}
        fw.set_watch_root(new_root)
        return {"status": "ok", "watch_root": str(new_root)}

    # ---------------------------------------------------------------------------
    # Resources
    # ---------------------------------------------------------------------------

    @mcp.resource("context://stable", description="Current stable prefix block (read-only)")
    def resource_stable() -> str:
        return assembler.assemble_stable_prefix()

    @mcp.resource("context://dirty", description="Current dirty files with diffs or full content")
    def resource_dirty() -> str:
        return assembler.assemble_dirty_section()

    @mcp.resource("context://state", description="Full watcher state as JSON")
    def resource_state() -> str:
        return json.dumps(
            {
                "watch_root": str(config.watch_root),
                "token_budget": config.token_budget,
                "diff_threshold": config.diff_threshold,
                "files": state.as_json(),
                "last_estimate": estimator.get_last_estimate().as_dict()
                if estimator.get_last_estimate()
                else None,
                **estimator.session_stats(),
            },
            indent=2,
        )

    return mcp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_path(path_str: str, watch_root: Path) -> Path:
    p = Path(path_str)
    if not p.is_absolute():
        p = watch_root / p
    return p.resolve()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="context-watcher MCP server")
    parser.add_argument(
        "--watch-root",
        type=Path,
        default=None,
        help="Directory to watch (default: current directory)",
    )
    parser.add_argument(
        "--token-budget",
        type=int,
        default=None,
        help="Maximum tokens in assembled context (default: 100,000)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    config = Config.load(watch_root=args.watch_root)
    if args.token_budget is not None:
        config.token_budget = args.token_budget

    mcp = _build_server(config)
    mcp.run()


if __name__ == "__main__":
    main()
