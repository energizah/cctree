# CLAUDE.md

## Project overview

canvas-claude is an external plugin for Canvas Chat that routes LLM calls through the Claude Code CLI. It consists of a Python backend plugin and a JavaScript frontend plugin, loaded via Canvas Chat's config-driven plugin system. Canvas Chat source lives at `~/lib/canvas-chat/` and is never modified.

## Commands

```bash
# Dev shell (creates .venv on first run)
nix develop

# Start server
uvicorn canvas_chat.app:app --reload --port 7865

# Run tests
pytest tests/ -v

# Lint
ruff check plugins/ tests/
```

The `nix develop` shell sets `CANVAS_CHAT_CONFIG_PATH` automatically. `LD_LIBRARY_PATH` is set for `libstdc++` (needed by the `tokenizers` package).

`cd` is unreliable in Claude's shell -- use full paths instead (e.g., `git -C /path/to/repo`).

## File layout

- `plugins/claude_code.py` -- Backend. Registers REST/SSE endpoints on the Canvas Chat FastAPI app. Imports `app` from `canvas_chat.app` at module level.
- `plugins/claude-code.js` -- Frontend. `ClaudeCodeFeature extends FeaturePlugin`. Self-registers via `window.addEventListener('app-plugin-system-ready', ...)`.
- `tui.py` -- Standalone TUI session tree viewer (`cctree`). Textual app that builds a trie from JSONL session files. Supports chat input (fork-rewind-resume), search (rg-backed), recent tip navigation, and `$EDITOR` composition.
- `dump_screenshots.py` -- Extracts and displays tree sections from TUI SVG screenshots for debugging.
- `tests/test_claude_code.py` -- 30 tests. Uses `tmp_path`, mocks filesystem. No server or CLI needed.
- `tests/test_tui.py` -- TUI tests for `_select_session` fork-point expansion.
- `config.yaml` -- Points Canvas Chat to the plugin files. Must have at least one model entry (placeholder, never used).
- `flake.nix` -- Nix dev shell. Python 3.11, uv, ruff, nodejs, libstdc++.
- `PLAN.md` -- Design document with architecture, data formats, edge cases.

## Key patterns

### Plugin loading

The Python plugin is loaded lazily -- `get_admin_config()` in `app.py` is called on the first `GET /` request, which triggers `load_python_plugins()`. Routes registered after this point work because FastAPI resolves routes dynamically. But any API call before the root page is visited will 404.

### anyio, not asyncio

Canvas Chat runs on uvicorn/starlette which uses anyio. Use anyio primitives:
- `anyio.open_process()` with `subprocess.PIPE` for stdout/stderr
- `process.stdout.receive(4096)` for reading (it's a `ByteReceiveStream`, not async iterable)
- `anyio.EndOfStream` for EOF detection
- `anyio.run_process()` for one-shot commands
- `anyio.open_file()` for async file I/O

### SSE event protocol

Backend yields dicts to `EventSourceResponse`:
```python
yield {"event": "message", "data": "text chunk"}
yield {"event": "status", "data": "Using tool: Read"}
yield {"event": "done", "data": json.dumps({"session_id": "...", "cost_usd": 0.01})}
yield {"event": "error", "data": "error message"}
```

Frontend reads via `readSSEStream()` from `/static/js/sse.js`.

### Claude Code stream-json format

Requires `--verbose` flag. Each stdout line from `claude --print --verbose --output-format stream-json` is a JSON object:
```
{"type":"assistant","message":{"content":[{"type":"text","text":"partial response..."}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{...}}]}}
{"type":"result","session_id":"<uuid>","total_cost_usd":0.01}
```

### JSONL session format

Files at `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`. Key record types: `user`, `assistant`, `file-history-snapshot`, `system`. Tree structure via `parentUuid`. Multiple consecutive assistant records with chained `parentUuid` are parts of the same turn -- coalesce into one canvas node.

### Frontend globals

External JS plugins access Canvas Chat internals via:
- `window.app` -- the App instance
- `window.app.featureRegistry` -- register features
- ES module imports from `/static/js/` (absolute paths, not relative)

## Testing conventions

- Tests import from `plugins.claude_code` directly (PYTHONPATH includes project root)
- Async endpoint functions are called directly with `asyncio.get_event_loop().run_until_complete()`
- Mock `_find_session_file` and `_sessions_dir` to use `tmp_path` fixtures
- No mocking of the Claude CLI -- test the parsing/conversion logic only
- TUI tests use Textual's `app.run_test()` with `pytest-asyncio`; poll `session_count` for worker completion

## Edge cases to be aware of

- `CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT` env vars must be stripped from subprocess env to avoid nested session errors
- `--dangerously-skip-permissions` is required for non-interactive `--print` mode
- `stdin=subprocess.DEVNULL` is required when spawning claude from uvicorn (otherwise the CLI hangs waiting on stdin)
- Fork can fail (network, CLI error) -- frontend falls back to using original session ID
- Large JSONL files: tool outputs are truncated via `_truncate()` during import
- The `first_prompt` in session listings strips XML system tags via regex
