# cctree

TUI session tree viewer for Claude Code conversations. Builds a trie from JSONL session files to detect shared prefixes (forks) and collapse linear chains.

```bash
cctree /path/to/project     # or just: cctree (uses cwd)
cctree --log                # enable logging/screenshots to temp dir
```

## Features

- **Trie-based merging**: shared message prefixes deduplicated, forks shown as branches
- **Lazy expansion**: chain segments populated on first expand
- **Chat input** (`i`): fork-rewind-resume via Claude CLI; tip replies skip forking
- **Compose in editor** (`Ctrl-e`): open `$EDITOR` to write messages
- **Search** (`/`): incremental label search + rg-backed full content search (`n`/`N`)
- **Recent tips** (`f`/`F`): navigate session endpoints sorted by recency
- **Message age**: timestamps on all nodes, color-coded by session recency
- **Detail panel** (`p`): sequential messages with syntax-highlighted code blocks

## Setup

Prerequisites:
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- Nix (for the dev shell)

```bash
git clone <this-repo> ~/src/canvas-claude
cd ~/src/canvas-claude
nix develop
```

Or run directly via Nix:

```bash
nix run github:<this-repo> -- /path/to/project
```

## Project structure

```
├── tui.py                # TUI session tree viewer (cctree)
├── dump_screenshots.py   # screenshot analysis tool
├── flake.nix             # nix develop shell
├── tests/
│   └── test_tui.py       # TUI tests
```
