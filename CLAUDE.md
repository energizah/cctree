# CLAUDE.md

## Project overview

cctree is a standalone TUI session tree viewer for Claude Code conversations. It builds a trie from JSONL session files to detect shared prefixes (forks) and collapse linear chains.

## Commands

```bash
# Dev shell (creates .venv on first run)
nix develop

# Run tests
pytest tests/ -v

# Lint
ruff check tui.py tests/
```

`cd` is unreliable in Claude's shell -- use full paths instead (e.g., `git -C /path/to/repo`).

## File layout

- `tui.py` -- Standalone TUI session tree viewer (`cctree`). Textual app that builds a trie from JSONL session files. Supports chat input (fork-rewind-resume), search (rg-backed), recent tip navigation, and `$EDITOR` composition.
- `dump_screenshots.py` -- Extracts and displays tree sections from TUI SVG screenshots for debugging.
- `tests/test_tui.py` -- TUI tests for `_select_session` fork-point expansion.
- `flake.nix` -- Nix dev shell. Python 3.11, uv, ruff.

## Key patterns

### JSONL session format

Files at `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`. Key record types: `user`, `assistant`, `file-history-snapshot`, `system`. Tree structure via `parentUuid`. Multiple consecutive assistant records with chained `parentUuid` are parts of the same turn -- coalesce into one canvas node.

## Testing conventions

- TUI tests use Textual's `app.run_test()` with `pytest-asyncio`; poll `session_count` for worker completion

## Edge cases to be aware of

- Large JSONL files: tool outputs are truncated via `_truncate()` during import
- The `first_prompt` in session listings strips XML system tags via regex
