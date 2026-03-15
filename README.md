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
- **Recent tips** (`t`/`T`): navigate session endpoints sorted by recency
- **Message age**: timestamps on all nodes, color-coded by session recency
- **Detail panel** (`p`): sequential messages with syntax-highlighted code blocks

## Keybindings

| Key | Action |
|-----|--------|
| `q` / `Ctrl-c` | Quit |
| `j` / `k` | Move cursor down / up |
| `h` / `l` | Collapse / expand node |
| `Ctrl-d` / `Ctrl-u` | Page down / up |
| `g` / `G` | Jump to top / bottom |
| `e` / `c` | Expand all / collapse all |
| `t` / `T` | Next / previous recent session tip |
| `p` | Toggle detail panel |
| `y` | Copy detail to clipboard |
| `o` | Open session in Claude CLI |
| `i` | Chat input (fork-rewind-resume) |
| `Ctrl-e` | Compose message in `$EDITOR` |
| `/` | Search |
| `n` / `N` | Next / previous search match |
| `r` | Reload sessions |

## Setup

Prerequisites:
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- Nix (for the dev shell)

```bash
git clone <this-repo>
cd cctree
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
