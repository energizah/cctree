# canvas-claude

Visual tree navigator for Claude Code conversations, built as an external plugin for [Canvas Chat](https://github.com/nicobailey/canvas-chat).

Canvas Chat provides an infinite SVG canvas where conversations branch as a DAG. This plugin routes all LLM calls through the Claude Code CLI instead of direct API calls, using your Max subscription with no API key needed.

## How it works

```
Canvas Chat UI (SVG canvas, vanilla JS)
    |  SSE / REST
Canvas Chat FastAPI backend
    |  anyio.open_process
claude --print --output-format stream-json --resume <session-id>
    |
Claude Code (Max subscription, JSONL on disk)
```

Each message exchange forks the Claude Code session, creating a snapshot at that point in the conversation. Branching from any node resumes from its fork, giving true tree-structured conversations backed by Claude Code's full tool-use capabilities.

## Setup

Prerequisites:
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated (`claude --version`)
- Canvas Chat source at `~/lib/canvas-chat/`
- Nix (for the dev shell)

```bash
git clone <this-repo> ~/src/canvas-claude
cd ~/src/canvas-claude
nix develop
# First run creates a .venv and installs canvas-chat + dependencies via uv.

uvicorn canvas_chat.app:app --reload --port 7865
# Open http://localhost:7865
```

Environment variables (all optional):
- `CANVAS_CHAT_DIR` -- path to canvas-chat source (default: `~/lib/canvas-chat`)
- `CANVAS_CLAUDE_DIR` -- path to this project (default: `~/src/canvas-claude`)

## Usage

### Slash commands

| Command | Description |
|---------|-------------|
| `/cc <prompt>` | Send a message via Claude Code CLI. Creates human + AI nodes, streams the response. |
| `/cc-import <session-id>` | Import a Claude Code JSONL session onto the canvas as a node tree. |
| `/cc-import` | (no args) Shows a session picker. |
| `/cc-sessions` | List available Claude Code sessions in a note node. |
| `/cc-cwd <path>` | Set the working directory for Claude Code subprocess calls. |
| `/cc-cwd` | (no args) Show the current working directory. |

### Branching

1. Type `/cc explain the auth module` -- creates a human node and streams the AI response.
2. Select the AI node and type `/cc now explain the tests` -- continues from that point.
3. Select the same AI node and type `/cc what about error handling` -- branches from the same point, creating a second conversation path.

Each AI node shows a colored dot indicating fork status:
- Green: ready to branch from
- Yellow (pulsing): fork in progress
- Red: fork failed (falls back to linear continuation)

### Importing existing sessions

Claude Code stores conversations as JSONL files in `~/.claude/projects/`. Import any session onto the canvas:

```
/cc-sessions              # list available sessions
/cc-import abc12345       # import by session ID (prefix match works)
```

The importer:
- Coalesces consecutive assistant records (thinking + tool_use + text) into single AI nodes
- Detects branches (multiple children of the same parent) and uses branch edges
- Lays out the tree via DFS with depth-based x positioning

## Project structure

```
~/src/canvas-claude/
├── README.md
├── CLAUDE.md                     # instructions for Claude sessions
├── PLAN.md                       # design document
├── flake.nix                     # nix develop shell
├── flake.lock
├── config.yaml                   # canvas-chat plugin config
├── plugins/
│   ├── claude_code.py            # backend plugin (550 lines)
│   └── claude-code.js            # frontend plugin (688 lines)
└── tests/
    └── test_claude_code.py       # 30 tests (459 lines)
```

Canvas Chat source at `~/lib/canvas-chat/` is not modified. Everything is loaded externally via the plugin system.

## Architecture

### Backend plugin (`claude_code.py`)

Registers 4 endpoints on the Canvas Chat FastAPI app:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/claude-code/chat` | POST | Stream a Claude Code response via SSE |
| `/api/claude-code/fork` | POST | Fork a session at its current tip |
| `/api/claude-code/import` | POST | Parse JSONL session into canvas nodes/edges |
| `/api/claude-code/sessions` | GET | List available sessions |

The chat endpoint spawns `claude --print --output-format stream-json`, translates stdout JSON lines into SSE events, and kills the subprocess on client disconnect. The `CLAUDECODE` env var is stripped to prevent nested session errors.

### Frontend plugin (`claude-code.js`)

`ClaudeCodeFeature extends FeaturePlugin` following Canvas Chat's plugin pattern. Self-registers via the `app-plugin-system-ready` event.

Key internal state:
- **ForkIndex** -- maps canvas node IDs to `{ sessionId, forkSessionId, claudeUuid }`, persisted in IndexedDB via the session object.
- **Pending forks** -- tracks in-flight fork operations so branching waits for completion.

### Fork-per-message lifecycle

1. User sends `/cc <prompt>`
2. Plugin looks up parent node's `forkSessionId` (or starts fresh)
3. Backend spawns `claude --resume <fork> --print --output-format stream-json "<prompt>"`
4. stream-json lines become SSE events, content streams into AI node
5. On completion, backend forks: `claude --resume <sid> --fork-session --print "."`
6. Plugin stores `{ sessionId, forkSessionId }` for the new AI node
7. To branch from this node later: resume from its `forkSessionId`

## Testing

```bash
nix develop
pytest tests/ -v
```

30 tests covering pure functions, JSONL import parsing (coalescing, branching, layout), and session listing. Tests use `tmp_path` fixtures and mock the filesystem -- no Claude CLI or running server needed.
