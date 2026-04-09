# context-watcher

A filesystem-watching, cache-aware context assembly service for LLM coding agents. Exposes an MCP server that delivers optimally ordered file context to maximize prefix cache hit rates and minimize token costs per request.

## The Problem

LLM inference re-reads every token in the context window on every request. Prefix caching (available via Anthropic, OpenAI, and self-hosted inference engines) reuses previously computed attention states — but only for token sequences that are byte-identical from position 0. A single changed file invalidates the cache for everything after it in the prompt.

Most coding tools assemble context naively: arbitrary file order, full file content every time, no awareness of what the provider has already cached. This means paying full token cost repeatedly for files that haven't changed.

**context-watcher** fixes this by:
- Tracking which files are stable vs dirty in real time
- Ordering stable files first (maximizing the cacheable prefix)
- Injecting only diffs for recently changed files (minimizing uncached tokens)
- Exposing this as an MCP tool so the agent controls what it fetches

## How It Works

```
Editor saves file
    ↓
inotify/watchdog detects change
    ↓
FileStateManager hashes file → marks dirty, computes diff vs last clean
    ↓
StabilityRanker updates ordering (most stable → least stable → dirty)
    ↓
[Agent makes request via MCP]
    ↓
PromptAssembler: stable files (ranked) + dirty files (diff or full) 
    ↓
Returns optimized context block + metadata
    ↓
API call → provider prefix cache hits on stable prefix
    ↓
CacheEstimator logs estimated saved tokens
```

### Diff vs Full File Heuristic

For dirty files: if the diff is under ~20% of the file's token count, inject the diff. Otherwise inject the full file. The model reasons better with full content for large changes; diffs are cheaper for small ones.

### Stability Score

Each file gets a score based on recency of last change. Files untouched longest rank highest in the stable prefix. Scores persist to disk between sessions so the watcher doesn't start cold.

## Architecture

```
context_watcher/
├── watcher.py          # Filesystem event loop (watchdog)
├── state.py            # FileStateManager: hash map, dirty flags, diffs
├── ranker.py           # StabilityRanker: ordering logic, score persistence
├── assembler.py        # PromptAssembler: builds context string
├── estimator.py        # CacheEstimator: token counting, hit rate logging
├── mcp_server.py       # MCP server exposing tools + resources
└── config.py           # Watch root, token budget, diff threshold, etc.
```

## MCP Interface

### Tools

```
get_context()
  → Returns fully assembled, cache-optimized context block
  → Stable files first (ranked), dirty files last (diff or full)
  → Respects configured token budget ceiling

get_dirty_files()
  → Returns list of files changed since last request
  → Use to decide whether to call get_context() at all

get_diff(path)
  → Returns unified diff for a specific dirty file

get_cache_estimate()
  → Returns estimated cached vs uncached tokens for last request
  → Useful for verifying the optimization is working

mark_stable(path)
  → Manually promote a file to stable prefix
  → Useful for files the agent knows won't change this session

set_watch_root(path)
  → Point watcher at a different directory
```

### Resources

```
context://stable     → Current stable prefix block (read-only)
context://dirty      → Current dirty files with diffs
context://state      → Full watcher state as JSON
```

## Key Design Constraints

**Deterministic serialization**: File content is always assembled in the same canonical order. Non-deterministic ordering silently breaks prefix caching even when content is identical.

**Append-only context**: The watcher never modifies already-assembled context. New information always goes at the tail.

**Token budget enforcement**: If stable + dirty + conversation tail would exceed the configured context window, the watcher drops least-stable files first, never silently truncating mid-file.

**No LLM calls**: The watcher is a pure local service. No inference dependency, no API cost, no latency from model calls. Just filesystem I/O, hashing, and string assembly.

## What This Is Not

- Not a code intelligence tool (no AST parsing, no import graph, no LSP)
- Not a memory system (no cross-session semantic retrieval — that's Graphiti's job)
- Not a summarizer (no compaction — that's a future layer)
- Not git-aware in v1 (file saves trigger it, not commits)

## Future Layers (Out of Scope for v1)

These compose with the watcher but are separate projects:

- **Graphiti ingestion**: feed diffs as episodes into a temporal knowledge graph on git commit events. Enables cross-session architectural memory and graph-informed stability scoring.
- **Small model compactor**: when conversation tail exceeds a threshold, a cheap local model summarizes it. The compressed output becomes the new stable prefix anchor.
- **Git integration**: use commit state to inform stability scores. Committed = more stable than staged.
- **Function-level granularity**: track dirty at the function level, not file level. Only the changed function is injected, rest of file stays in stable prefix.
- **Import graph awareness**: a change to a dependency marks dependents as semantically dirty even if their bytes didn't change.

## Token Cost Impact

Prefix caching discounts (as of early 2026):
- Anthropic: cached input tokens billed at ~10% of normal input rate
- OpenAI: similar discount on cached prefix tokens

In active coding sessions with a large stable codebase, the majority of input tokens should be in the cached prefix. The watcher's job is to maximize that ratio. Instrumentation via `get_cache_estimate()` lets you verify it's working.

## Implementation Notes for Claude Code

- Use `watchdog` for filesystem events (cross-platform inotify/FSEvents/kqueue)
- Use `tiktoken` or Anthropic's tokenizer for accurate token counting (load-bearing for budget enforcement and diff heuristic)
- Persist stability scores as a simple JSON sidecar (`.context_watcher_state.json`) in the watch root
- Diffs via Python's `difflib.unified_diff` — no external dependency
- MCP server via `mcp` Python SDK
- Start with a single configurable watch root; multi-root is a later concern
- Config via a simple `.context_watcher.toml` in the watch root or home directory

## Success Metrics

- Cache hit rate per session (tracked by estimator, logged to stdout or file)
- Token cost with vs without watcher on identical coding sessions
- Model correctness on diff-injected context vs full file (manual spot-check)
