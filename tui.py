"""Claude Code Session Tree TUI.

Interactive terminal tree view of all Claude Code sessions under a working
directory.  Builds a trie from JSONL session files to detect shared prefixes
(forks) and collapse linear chains, then renders the result in a Textual
Tree widget.

Usage:
    cctree /path/to/project
    cctree                          # uses cwd
    cctree --help / --version

Keybindings:
    j/k         cursor down/up
    h/l         collapse/expand node
    Ctrl-d/u    page down/up
    g/G         top/bottom
    e/c         expand all / collapse all
    p           toggle detail panel
    y           copy detail to clipboard
    i           focus chat input
    /           search (incremental on labels, Enter for full content)
    n/N         next/prev search match (expands collapsed nodes)
    o           open selected session in claude --resume
    r           reload sessions from disk
    Escape      cancel search or return to tree from input
    q           quit

Features:
    - Trie-based session merging: shared message prefixes across sessions
      are deduplicated, forks shown as branches
    - Lazy expansion: chain segments and trie children are populated on
      first expand, keeping startup fast
    - Detail panel: shows sequential messages up to viewport height with
      syntax-highlighted code blocks (Pygments via Rich)
    - Chat input (i): send a prompt that forks the selected session,
      rewinds to the selected message, and resumes via claude --print
    - Search (/): Helix-style incremental search on labels while typing;
      Enter or n/N triggers full content search backed by rg/grep for
      fast session-file narrowing, then regex match on message content
    - Search navigation: saves/restores collapse state so expanded
      subtrees are collapsed when moving to the next match
    - Emoji role indicators (✨ assistant, 👤 human,
      🛠️ tool result)
    - Status bar: session metadata (date range, model, session ID)
      on highlighted node
    - Search match highlighting in detail panel

Ideas / TODO:
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_log = logging.getLogger("cctree")
_log.setLevel(logging.DEBUG)
_log.addHandler(logging.NullHandler())

_SCREENSHOT_DIR: Path | None = None
_LOG_DIR: Path | None = None
_screenshot_seq = 0


def _enable_logging() -> Path:
    """Enable file logging and screenshots in a temporary directory.

    Returns the log directory path.
    """
    global _SCREENSHOT_DIR, _LOG_DIR
    import tempfile
    _LOG_DIR = Path(tempfile.mkdtemp(prefix="cctree-"))
    _SCREENSHOT_DIR = _LOG_DIR / "snap"
    _SCREENSHOT_DIR.mkdir()

    handler = logging.FileHandler(_LOG_DIR / "cctree.log")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(message)s", datefmt="%H:%M:%S"
    ))
    _log.addHandler(handler)
    return _LOG_DIR

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Input, Static, Tree
from textual import work


def _encode_cwd(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\-]", "-", cwd)


def _sessions_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _find_session_file(session_id: str, cwd: str) -> Path | None:
    """Locate the JSONL file for a given session ID under cwd."""
    base = _sessions_dir()
    encoded = _encode_cwd(cwd)
    for d in base.iterdir():
        if not d.is_dir() or not d.name.startswith(encoded):
            continue
        p = d / f"{session_id}.jsonl"
        if p.exists():
            return p
    return None


def _rewind_session_file(path: Path, target_hash: str) -> bool:
    """Truncate a session JSONL in-place up to the message matching target_hash.

    Replays coalescing to compute content hashes, finds the raw line matching
    the target, and rewrites the file with only records up to that point.
    Returns True on success.
    """
    with open(path) as f:
        raw_lines = f.readlines()

    # Parse user/assistant records, tracking line indices
    records = []  # (rec, line_idx)
    record_by_uuid = {}

    for i, line in enumerate(raw_lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            rec = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if rec.get("type") not in ("user", "assistant"):
            continue
        records.append((rec, i))
        uid = rec.get("uuid")
        if uid:
            record_by_uuid[uid] = rec

    # Replay coalescing to compute content hashes per coalesced message
    coalesced = []  # list of {message, _root_uuid, _last_line}
    merge_target = {}

    for rec, line_idx in records:
        rtype = rec.get("type")
        uid = rec.get("uuid")
        parent_uuid = rec.get("parentUuid")

        if rtype == "assistant" and parent_uuid and parent_uuid in record_by_uuid:
            parent_rec = record_by_uuid[parent_uuid]
            if parent_rec.get("type") == "assistant":
                root = parent_uuid
                while root in merge_target:
                    root = merge_target[root]
                merge_target[uid] = root

                for entry in coalesced:
                    if entry["_root_uuid"] == root:
                        entry["_last_line"] = line_idx
                        root_msg = entry["message"]
                        root_content = root_msg.get("content", [])
                        new_content = rec.get("message", {}).get("content", [])
                        if isinstance(root_content, str):
                            root_content = [{"type": "text", "text": root_content}]
                        if isinstance(new_content, str):
                            new_content = [{"type": "text", "text": new_content}]
                        root_msg["content"] = root_content + new_content
                        break
                continue

        if rtype == "user" or (rtype == "assistant" and uid not in merge_target.values()):
            if uid not in merge_target:
                coalesced.append({
                    "type": rtype,
                    "message": {**rec.get("message", {})},
                    "_root_uuid": uid or "",
                    "_last_line": line_idx,
                })

    # Find cutoff line for target_hash
    cutoff_line = None
    all_hashes = []
    for entry in coalesced:
        msg = entry["message"]
        role = msg.get("role", "")
        content = msg.get("content", "")
        raw = json.dumps(content, sort_keys=True)
        h = hashlib.sha256(f"{role}:{raw}".encode()).hexdigest()[:16]
        all_hashes.append(h)
        cutoff_line = entry["_last_line"]
        if h == target_hash:
            break
    else:
        _log.info(f"rewind: target {target_hash} not in {len(all_hashes)} hashes")
        _log.info(f"rewind: last 5 hashes: {all_hashes[-5:]}")
        return False

    # Rewrite file truncated at cutoff
    with open(path, "w") as f:
        for i, line in enumerate(raw_lines):
            if i > cutoff_line:
                break
            f.write(line)

    return True



def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n\n... (truncated, {len(text)} chars total)"


def _extract_text_content(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                name = block.get("name", "unknown")
                inp = block.get("input", {})
                inp_str = json.dumps(inp, indent=2) if isinstance(inp, dict) else str(inp)
                parts.append(f"**Tool: {name}**\n```json\n{inp_str}\n```")
            elif btype == "tool_result":
                out = block.get("output", block.get("content", ""))
                if isinstance(out, list):
                    out = "\n".join(
                        b.get("text", "") for b in out if isinstance(b, dict)
                    )
                parts.append(f"**Tool result:**\n```\n{out}\n```")
    return "\n\n".join(parts)


def _parse_session_file(path: Path) -> list[dict]:
    records: list[dict] = []
    record_by_uuid: dict[str, dict] = {}

    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = rec.get("type")
            if rtype not in ("user", "assistant"):
                continue
            records.append(rec)
            uuid = rec.get("uuid")
            if uuid:
                record_by_uuid[uuid] = rec

    coalesced: list[dict] = []
    merge_target: dict[str, str] = {}

    for rec in records:
        rtype = rec.get("type")
        uuid = rec.get("uuid")
        parent_uuid = rec.get("parentUuid")

        if rtype == "assistant" and parent_uuid and parent_uuid in record_by_uuid:
            parent_rec = record_by_uuid[parent_uuid]
            if parent_rec.get("type") == "assistant":
                root = parent_uuid
                while root in merge_target:
                    root = merge_target[root]
                merge_target[uuid] = root

                root_entry = None
                for entry in coalesced:
                    if entry.get("_root_uuid") == root:
                        root_entry = entry
                        break
                if root_entry is None:
                    root_rec = record_by_uuid[root]
                    root_entry = {**root_rec, "_root_uuid": root}
                    for i, entry in enumerate(coalesced):
                        if entry.get("uuid") == root:
                            coalesced[i] = root_entry
                            break

                root_msg = root_entry["message"]
                root_content = root_msg.get("content", [])
                new_content = rec.get("message", {}).get("content", [])
                if isinstance(root_content, str):
                    root_content = [{"type": "text", "text": root_content}]
                if isinstance(new_content, str):
                    new_content = [{"type": "text", "text": new_content}]
                root_msg["content"] = root_content + new_content
                continue

        if rtype == "user" or (rtype == "assistant" and uuid not in merge_target.values()):
            if uuid not in merge_target:
                coalesced.append(rec)

    result = []
    for rec in coalesced:
        msg = rec.get("message", {})
        role = msg.get("role", "")
        content = msg.get("content", "")
        raw = json.dumps(content, sort_keys=True)
        h = hashlib.sha256(f"{role}:{raw}".encode()).hexdigest()[:16]
        entry = {
            "type": rec.get("type"),
            "message": msg,
            "timestamp": rec.get("timestamp", ""),
            "content_hash": h,
            "session_id": rec.get("sessionId", ""),
        }
        model = msg.get("model")
        if model:
            entry["model"] = model
        result.append(entry)

    return result


# ---------------------------------------------------------------------------
# Trie builder (extracted from import-dag endpoint)
# ---------------------------------------------------------------------------


def _build_trie(cwd: str) -> tuple[dict, int, dict[str, str]]:
    """Build a trie from all sessions under cwd.

    Returns (trie_root, session_count, session_tips) where session_tips
    maps session_id -> ISO timestamp of the last message in that session.
    """
    base = _sessions_dir()
    if not base.exists():
        return {"children": {}, "messages": [], "count": 0, "session_ids": set()}, 0, {}

    encoded = _encode_cwd(cwd)
    dirs = [d for d in base.iterdir() if d.is_dir() and d.name.startswith(encoded)]

    all_sessions: list[list[dict]] = []
    session_tips: dict[str, str] = {}  # session_id -> last timestamp
    for d in dirs:
        for jsonl_file in d.glob("*.jsonl"):
            msgs = _parse_session_file(jsonl_file)
            if msgs:
                file_sid = jsonl_file.stem
                for msg in msgs:
                    msg["file_session_id"] = file_sid
                all_sessions.append(msgs)
                # Track the last timestamp for this session
                last_ts = msgs[-1].get("timestamp", "")
                if last_ts:
                    session_tips[file_sid] = last_ts

    if not all_sessions:
        return {"children": {}, "messages": [], "count": 0, "session_ids": set()}, 0, {}

    all_sessions.sort(key=len, reverse=True)

    trie_root: dict = {"children": {}, "messages": [], "count": 0, "session_ids": set()}

    for session_msgs in all_sessions:
        node = trie_root
        for msg in session_msgs:
            h = msg["content_hash"]
            if h not in node["children"]:
                node["children"][h] = {
                    "children": {},
                    "messages": [msg],
                    "count": 0,
                    "session_ids": set(),
                }
            node = node["children"][h]
            node["count"] += 1
            node["session_ids"].add(msg.get("file_session_id", msg["session_id"]))

    return trie_root, len(all_sessions), session_tips


def _msg_role(msg: dict) -> tuple[str, str]:
    """Return (role_char, style) for a message, distinguishing tool results from human."""
    if msg["type"] == "assistant":
        return "✨", ""
    # "user" type — check if it's actually a tool_result
    content = msg["message"].get("content", "")
    if isinstance(content, list):
        if all(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in content
        ):
            return "🛠️", ""
    return "👤", ""


def _age_text(iso_ts: str) -> str:
    """Return human-readable age string for an ISO timestamp."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
    except (ValueError, TypeError):
        return ""
    if secs < 0:
        return ""
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _age_style(tip_ts: str, is_tip: bool) -> str:
    """Return style for an age label based on session recency.

    Tip node of a recent session: bold green.
    Ancestor on a recent session's path: green (not bold).
    Everything else: dim.
    """
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(tip_ts.replace("Z", "+00:00"))
        recent = (datetime.now(timezone.utc) - dt).total_seconds() < 3600
    except (ValueError, TypeError):
        recent = False
    if not recent:
        return "dim"
    return "bold green" if is_tip else "#98c379"


def _preview(msg: dict) -> str:
    """One-line preview of a message (no truncation — tree widget clips)."""
    content = msg["message"].get("content", "")
    if isinstance(content, str):
        preview = content
    else:
        preview = ""
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                preview = block.get("text", "")
                break
            if isinstance(block, dict) and block.get("type") == "tool_use":
                preview = f"[{block.get('name', 'tool')}]"
                break
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_content = block.get("content", block.get("output", ""))
                if isinstance(tool_content, list):
                    tool_content = " ".join(
                        b.get("text", "") for b in tool_content if isinstance(b, dict)
                    )
                preview = str(tool_content)
                break
    preview = preview.replace("\n", " ").strip()
    return preview


# ---------------------------------------------------------------------------
# Message detail panel
# ---------------------------------------------------------------------------


def _format_detail(msg: dict, max_chars: int = 2000) -> Text:
    """Format a message for the detail panel as highlighted Rich Text."""
    from rich.syntax import Syntax

    role = msg.get("type", "unknown")
    content = _extract_text_content(msg.get("message", {}))
    content = _truncate(content, max_chars)
    session = msg.get("session_id", "?")
    ts = msg.get("timestamp", "")

    result = Text()
    result.append(f"[{role.upper()}]", style="bold")
    result.append(f"  session={session}  ts={ts}\n", style="dim")
    result.append("=" * 60 + "\n", style="dim")

    # Split on fenced code blocks and highlight them
    parts = re.split(r"(```\w*\n.*?```)", content, flags=re.DOTALL)
    for part in parts:
        m = re.match(r"```(\w*)\n(.*?)```", part, flags=re.DOTALL)
        if m:
            lang = m.group(1) or "text"
            code = m.group(2)
            # For Write/Edit tool JSON, extract embedded code by file extension
            if lang == "json":
                code, lang = _extract_tool_code(code, lang)
            syntax = Syntax(code, lang, theme="monokai", line_numbers=False)
            result.append_text(syntax.highlight(code))
        else:
            result.append(part)

    return result


# Map file extensions to Pygments language names
_EXT_TO_LANG = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".jsx": "jsx", ".tsx": "tsx", ".jl": "julia", ".rs": "rust",
    ".go": "go", ".rb": "ruby", ".sh": "bash", ".bash": "bash",
    ".zsh": "zsh", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".json": "json", ".md": "markdown", ".html": "html", ".css": "css",
    ".sql": "sql", ".lua": "lua", ".nix": "nix", ".c": "c",
    ".cpp": "cpp", ".h": "c", ".hpp": "cpp", ".java": "java",
    ".kt": "kotlin", ".swift": "swift", ".ex": "elixir",
    ".erl": "erlang", ".hs": "haskell", ".ml": "ocaml",
}


def _extract_tool_code(code: str, fallback_lang: str) -> tuple[str, str]:
    """For Write/Edit tool JSON, pull out the embedded code and detect language."""
    try:
        obj = json.loads(code)
    except (json.JSONDecodeError, ValueError):
        # JSON string values may contain unescaped newlines from content extraction;
        # re-escape them inside quoted strings and retry
        fixed = re.sub(
            r'"((?:[^"\\]|\\.)*?)"',
            lambda m: '"' + m.group(1).replace("\n", "\\n").replace("\t", "\\t") + '"',
            code,
            flags=re.DOTALL,
        )
        try:
            obj = json.loads(fixed)
        except (json.JSONDecodeError, ValueError):
            return code, fallback_lang

    if not isinstance(obj, dict):
        return code, fallback_lang

    file_path = obj.get("file_path", "")
    ext = Path(file_path).suffix.lower() if file_path else ""
    lang = _EXT_TO_LANG.get(ext, fallback_lang)

    # Write tool: "content" has the full file
    if "content" in obj:
        return obj["content"], lang

    # Edit tool: show old_string -> new_string
    old = obj.get("old_string", "")
    new = obj.get("new_string", "")
    if old or new:
        parts = []
        if old:
            parts.append(f"--- old ---\n{old}")
        if new:
            parts.append(f"+++ new +++\n{new}")
        return "\n\n".join(parts), lang

    return code, fallback_lang


# ---------------------------------------------------------------------------
# Textual App
# ---------------------------------------------------------------------------


class SessionTreeApp(App):
    """Interactive session tree viewer."""

    TITLE = "Claude Code Sessions"
    CSS = """
    #tree {
        width: 1fr;
        min-width: 40;
        border-right: solid $accent;
    }
    #detail {
        display: none;
        width: 2fr;
        padding: 1 2;
        overflow-y: auto;
    }
    #status {
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    #main {
        layout: horizontal;
        height: 1fr;
    }
    #chat-input {
        margin: 0 1;
    }
    #search-input {
        display: none;
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("e", "expand_all", "Expand all"),
        Binding("c", "collapse_all", "Collapse all"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("h", "collapse_node", "Collapse", show=False),
        Binding("l", "expand_node", "Expand", show=False),
        Binding("ctrl+d", "page_down", "Page down", show=False),
        Binding("ctrl+u", "page_up", "Page up", show=False),
        Binding("g", "go_top", "Top", show=False),
        Binding("G", "go_bottom", "Bottom", show=False),
        Binding("p", "toggle_detail", "Toggle detail"),
        Binding("y", "yank_detail", "Copy detail"),
        Binding("i", "focus_input", "Chat"),
        Binding("o", "open_session", "Open in claude"),
        Binding("r", "reload", "Reload"),
        Binding("ctrl+e", "edit_message", "Edit in $EDITOR", show=False),
        Binding("slash", "search", "Search", show=False),
        Binding("n", "search_next", "Next match", show=False),
        Binding("N", "search_prev", "Prev match", show=False),
        Binding("f", "recent_next", "Recent"),
        Binding("F", "recent_prev", "Prev recent tip", show=False),
    ]

    def __init__(self, cwd: str):
        super().__init__()
        self.cwd = cwd
        self.title = f"Claude Code Sessions — {cwd}"
        self.trie_root: dict = {}
        self.session_count = 0
        self.session_tips: dict[str, str] = {}
        self._pending_detail = None
        self._detail_timer = None
        self._node_data: dict[int, dict] = {}  # id -> heavy data
        self._next_id = 0
        self._streaming = False
        self._loading = True
        self._search_matches: list = []
        self._search_index: int = -1
        self._search_pattern: str = ""
        self._pre_search_expanded: set = set()
        self._search_expanded: set = set()
        self._recent_tips: list[str] = []  # session IDs, sorted most-recent-first
        self._recent_index: int = -1

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            yield Tree("Sessions", id="tree")
            yield Static("Select a node to view details", id="detail")
        yield Input(
            placeholder="Send a message (Enter to send, Ctrl+E to edit in $EDITOR)",
            id="chat-input",
        )
        yield Input(placeholder="/", id="search-input")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._snap(f"mounted cwd={self.cwd}")
        tree = self.query_one("#tree", Tree)
        tree.root.set_label(Text(f" {self.cwd}", style="bold"))
        tree.root.expand()
        tree.root.add_leaf(Text("Loading sessions...", style="dim italic"))
        status = self.query_one("#status", Static)
        status.update(" Loading sessions...")
        self.load_tree()

    @work(thread=True, group="load_tree")
    def load_tree(self, select_session: str | None = None) -> None:
        """Parse sessions in a worker thread so the UI stays responsive."""
        _log.info(f"load_tree start select={select_session and select_session[:8]}")
        self.trie_root, self.session_count, self.session_tips = _build_trie(self.cwd)
        _log.info(f"load_tree trie built")
        self.call_from_thread(self._render_tree, select_session)

    def _render_tree(self, select_session: str | None = None) -> None:
        self._snap(f"_render_tree start select={select_session and select_session[:8]}")
        self._recent_tips = []
        self._recent_index = -1
        tree = self.query_one("#tree", Tree)
        tree.root.remove_children()
        self._add_trie_children(tree.root, self.trie_root)

        self._loading = False
        status = self.query_one("#status", Static)
        node_count = self._count_nodes(self.trie_root)
        status.update(f" {self.session_count} sessions, {node_count} messages")
        self._snap(f"_render_tree done")

        if select_session:
            self._select_session(select_session)

    def _count_nodes(self, trie_node: dict) -> int:
        count = 0
        stack = [trie_node]
        while stack:
            node = stack.pop()
            for child in node["children"].values():
                count += 1
                stack.append(child)
        return count

    def _store(self, data: dict) -> int:
        """Store heavy data in side dict, return lightweight ID."""
        nid = self._next_id
        self._next_id += 1
        self._node_data[nid] = data
        return nid

    def _get(self, nid) -> dict | None:
        """Retrieve heavy data by ID."""
        if isinstance(nid, int):
            return self._node_data.get(nid)
        return None

    def _snap(self, msg: str) -> None:
        """Log a message and save an SVG screenshot (no-op if --log not set)."""
        if _SCREENSHOT_DIR is None:
            return
        global _screenshot_seq
        _log.info(msg)
        try:
            svg = self.export_screenshot(title=msg)
            fname = _SCREENSHOT_DIR / f"{_screenshot_seq:04d}.svg"
            _screenshot_seq += 1
            fname.write_text(svg)
            _log.info(f"  screenshot -> {fname.name}")
        except Exception as e:
            _log.info(f"  screenshot failed: {e}")

    def _select_session(self, session_id: str) -> None:
        """Navigate to the fork point for a session and select it.

        Only expands fork points (nodes with multiple session-bearing
        children) and the final target's parent — intermediate chain
        nodes are populated for walking but stay collapsed.
        """
        self._snap(f"_select_session {session_id[:8]}")
        tree = self.query_one("#tree", Tree)

        def _walk(node):
            """DFS: find deepest node containing session_id, populating along the way."""
            best = None
            for child in list(node.children):
                data = self._get(child.data)
                if not data:
                    continue
                if session_id not in data.get("session_ids", []):
                    continue

                # Populate children so we can walk deeper, but don't expand yet.
                self._populate_placeholder(child)

                # Try to go deeper
                deeper = _walk(child)
                best = deeper or child

            return best

        target = _walk(tree.root)
        if not target:
            return

        # Collect ancestors from target to root
        ancestors = []
        node = target
        while node is not None and node is not tree.root:
            ancestors.append(node)
            node = node.parent
        ancestors.reverse()  # root-first order

        # Expand ancestors on the path so the target is visible.
        for anc in ancestors:
            self._populate_placeholder(anc)
            if anc is not target:
                anc.expand()
                self._snap(f"expanded node={anc.data} label={anc.label.plain[:40]}")

        def _finish_select():
            tree.select_node(target)
            tree.scroll_to_node(target)
        self.call_after_refresh(_finish_select)

    def _populate_placeholder(self, node, *, tail_only: bool = False) -> None:
        """Replace placeholder child with real chain segments and trie children.

        If *tail_only* is True and the node has a chain, only the last segment
        (the fork point) is shown; the earlier segments are collapsed into a
        single ``[N earlier msgs]`` summary node.  This avoids flooding the
        tree with dozens of individual messages when navigating to a session.
        """
        data = self._get(node.data)
        if not data:
            return

        children = list(node.children)
        if len(children) != 1 or children[0].data != -1:
            return  # already populated

        self._snap(f"populate node={node.data} label={node.label.plain[:40]}")
        children[0].remove()

        chain = data.get("chain")
        trie_node = data.get("_trie_node")

        if chain:
            # Determine which segments to render individually
            if tail_only and len(chain) > 1 and trie_node:
                # Collapse the first N-1 segments into a summary node
                head = chain[:-1]
                head_session_ids: set[str] = set()
                for seg in head:
                    head_session_ids.update(seg["session_ids"])
                head_session_ids.discard("")
                head_msgs = [seg["messages"][0] for seg in head]
                summary_label = Text()
                summary_label.append(
                    f"[{len(head)} earlier msgs]", style="dim italic"
                )
                summary_data = {
                    "session_ids": sorted(head_session_ids),
                    "first_msg": head_msgs[0],
                    "last_msg": head_msgs[-1],
                    "msgs": head_msgs,
                    "msg_count": len(head),
                    "count": head[0]["count"],
                    "chain": head,
                    "_trie_node": None,
                }
                snid = self._store(summary_data)
                summary_node = node.add(summary_label, data=snid)
                summary_node.add_leaf(Text("...", style="dim"), data=-1)

                # Only render the last segment (fork point)
                segments_to_render = [chain[-1]]
            else:
                segments_to_render = chain

            for i, seg in enumerate(segments_to_render):
                msg = seg["messages"][0]
                preview_text = _preview(msg)
                role, role_style = _msg_role(msg)
                seg_session_ids = sorted(seg["session_ids"] - {""})

                # Age prefix — message's own timestamp, styled by session recency
                msg_ts = msg.get("timestamp", "")
                best_tip_ts = max(
                    (self.session_tips.get(sid, "") for sid in seg_session_ids),
                    default="",
                )
                msg_label = Text()
                is_tip = (seg is chain[-1]) and not trie_node
                msg_age = _age_text(msg_ts)
                if msg_age:
                    style = _age_style(best_tip_ts, is_tip)
                    msg_label.append(f"({msg_age}) ", style=style)

                msg_data = {
                    "session_ids": seg_session_ids,
                    "first_msg": msg,
                    "last_msg": msg,
                    "msg_count": 1,
                    "count": seg["count"],
                }

                is_last_of_chain = (seg is chain[-1])
                if is_last_of_chain and trie_node:
                    # Last segment gets trie branches as children
                    n_branches = len(trie_node["children"])
                    if n_branches > 1:
                        msg_label.append(f"[{n_branches} branches] ", style="bold yellow")
                    msg_label.append(f"{role}: ", style=role_style)
                    msg_label.append(preview_text)
                    msg_data["_trie_node"] = trie_node
                    nid = self._store(msg_data)
                    last_node = node.add(msg_label, data=nid)
                    last_node.add_leaf(Text("...", style="dim"), data=-1)
                else:
                    msg_label.append(f"{role}: ", style=role_style)
                    msg_label.append(preview_text)
                    nid = self._store(msg_data)
                    node.add_leaf(msg_label, data=nid)
            data["chain"] = None
            data["_trie_node"] = None
        elif trie_node:
            self._add_trie_children(node, trie_node)
            data["_trie_node"] = None

    def _expand_node_now(self, node) -> None:
        """Eagerly populate and expand a node (bypasses async message dispatch)."""
        self._populate_placeholder(node)
        node.expand()

    def _add_trie_children(self, parent_tree_node, trie_node: dict) -> None:
        """Add one level of trie children to a Textual tree node (lazy)."""
        for _h, child in trie_node["children"].items():
            # Collapse linear chains
            chain = [child]
            cur = child
            while len(cur["children"]) == 1:
                cur = next(iter(cur["children"].values()))
                chain.append(cur)

            msg = chain[0]["messages"][0]
            preview_text = _preview(msg)
            count = chain[0]["count"]
            n_msgs = len(chain)

            end_node = chain[-1]
            role, role_style = _msg_role(msg)
            n_branches = len(end_node["children"])
            has_children = bool(end_node["children"])

            chain_session_ids: set[str] = set()
            for seg in chain:
                chain_session_ids.update(seg["session_ids"])
            chain_session_ids.discard("")

            label = Text()

            # Age prefix — message's own timestamp, styled by session recency
            msg_ts = msg.get("timestamp", "")
            best_tip_ts = max(
                (self.session_tips.get(sid, "") for sid in chain_session_ids),
                default="",
            )
            msg_age = _age_text(msg_ts)
            if msg_age:
                style = _age_style(best_tip_ts, n_msgs == 1 and not has_children)
                label.append(f"({msg_age}) ", style=style)

            if n_msgs == 1 and n_branches > 1:
                label.append(f"[{n_branches} branches] ", style="bold yellow")
                label.append(f"{role}: ", style=role_style)
                label.append(preview_text)
            elif n_msgs > 1:
                label.append(f"[{n_msgs} msgs] ", style="bold yellow")
                label.append(f"{role}: ", style=role_style)
                label.append(preview_text)
            else:
                label.append(f"{role}: ", style=role_style)
                label.append(preview_text)

            if count > 1:
                label.append(f"  \u00d7{count}", style="dim")

            all_msgs = [seg["messages"][0] for seg in chain]
            data = {
                "session_ids": sorted(chain_session_ids),
                "first_msg": chain[0]["messages"][0],
                "last_msg": chain[-1]["messages"][0],
                "msgs": all_msgs,
                "msg_count": n_msgs,
                "count": count,
                "chain": chain if n_msgs > 1 else None,
                "_trie_node": end_node if has_children else None,
                "_hash_key": _h,
            }
            nid = self._store(data)

            if has_children or n_msgs > 1:
                child_tree_node = parent_tree_node.add(label, data=nid)
                child_tree_node.add_leaf(
                    Text("...", style="dim"),
                    data=-1,  # placeholder sentinel
                )
            else:
                parent_tree_node.add_leaf(label, data=nid)

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        """Lazily populate children on first expand."""
        self._snap(f"expanded node={event.node.data} label={event.node.label.plain[:40]}")
        self._populate_placeholder(event.node)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        """Show message detail when cursor moves to a node."""
        self._snap(f"highlighted: node.data={event.node.data}")
        data = self._get(event.node.data)
        self._pending_detail = data
        if self._detail_timer:
            self._detail_timer.stop()
        self._detail_timer = self.set_timer(0.02, self._flush_detail)
        self._update_status_bar(data)

    def _flush_detail(self) -> None:
        self._snap("flush_detail fired")
        detail = self.query_one("#detail", Static)
        data = self._pending_detail
        if not data:
            detail.update("(root node)")
            return

        session_ids = data.get("session_ids", [])
        msg_count = data.get("msg_count", 1)
        count = data.get("count", 1)

        result = Text()
        result.append(f"Sessions: {', '.join(s[:8] for s in session_ids)}\n")
        result.append(f"Messages in chain: {msg_count}\n")
        if count > 1:
            result.append(f"Shared across: {count} sessions\n")
        result.append("\n")

        msgs = data.get("msgs") or []
        if not msgs:
            first_msg = data.get("first_msg")
            if first_msg:
                msgs = [first_msg]

        # Lazy: only format enough messages to fill the visible area
        budget = max(self.size.height - 6, 10)  # lines available
        lines_used = 0
        for i, msg in enumerate(msgs):
            if i > 0:
                if lines_used >= budget:
                    remaining = len(msgs) - i
                    result.append(f"\n\n... {remaining} more message(s)", style="dim")
                    break
                result.append("\n\n")
                lines_used += 2
            formatted = _format_detail(msg)
            lines_used += formatted.plain.count("\n") + 1
            result.append_text(formatted)

        # Highlight search matches in the detail panel
        if self._search_pattern:
            regex = self._compile_pattern(self._search_pattern)
            if regex:
                result.highlight_regex(regex.pattern, style="bold reverse yellow")

        detail.update(result)

    def _update_status_bar(self, data: dict | None) -> None:
        """Update status bar with session metadata for the highlighted node."""
        status = self.query_one("#status", Static)
        if self._loading:
            status.update(" Loading sessions...")
            return
        base = f" {self.session_count} sessions"

        if not data:
            status.update(base)
            return

        parts = [base]

        # Date range from first/last message timestamps
        first_ts = (data.get("first_msg") or {}).get("timestamp", "")
        last_ts = (data.get("last_msg") or {}).get("timestamp", "")
        if first_ts:
            # Format: just date+time, drop timezone
            first_short = first_ts[:16].replace("T", " ") if "T" in first_ts else first_ts[:16]
            if last_ts and last_ts != first_ts:
                last_short = last_ts[:16].replace("T", " ") if "T" in last_ts else last_ts[:16]
                parts.append(f"{first_short} .. {last_short}")
            else:
                parts.append(first_short)

        # Session ID(s) for this node
        sids = data.get("session_ids", [])
        if len(sids) == 1:
            parts.append(f"session {sids[0][:8]}")
        elif len(sids) > 1:
            parts.append(f"{len(sids)} sessions")

        # Model from messages
        msgs = data.get("msgs") or []
        if not msgs:
            first_msg = data.get("first_msg")
            if first_msg:
                msgs = [first_msg]
        models = {m.get("model") for m in msgs if m.get("model")}
        if models:
            parts.append(", ".join(sorted(models)))

        status.update(" │ ".join(parts))

    def action_cursor_down(self) -> None:
        self._snap("cursor_down")
        tree = self.query_one("#tree", Tree)
        tree.action_cursor_down()

    def action_cursor_up(self) -> None:
        self._snap("cursor_up")
        tree = self.query_one("#tree", Tree)
        tree.action_cursor_up()

    def action_expand_node(self) -> None:
        tree = self.query_one("#tree", Tree)
        node = tree.cursor_node
        if node and not node.is_expanded:
            node.expand()
        else:
            # If already expanded, move to first child
            tree.action_cursor_down()

    def action_collapse_node(self) -> None:
        tree = self.query_one("#tree", Tree)
        node = tree.cursor_node
        if node and node.is_expanded:
            node.collapse()
        elif node and node.parent:
            # Move to parent
            tree.select_node(node.parent)

    def action_page_down(self) -> None:
        tree = self.query_one("#tree", Tree)
        for _ in range(tree.size.height // 2):
            tree.action_cursor_down()

    def action_page_up(self) -> None:
        tree = self.query_one("#tree", Tree)
        for _ in range(tree.size.height // 2):
            tree.action_cursor_up()

    def action_go_top(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.scroll_home()
        if tree.root.children:
            tree.select_node(tree.root.children[0])

    def action_go_bottom(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.scroll_end()
        # Find last visible node
        node = tree.root
        while node.children:
            last = list(node.children)[-1]
            if not last.is_expanded:
                node = last
                break
            node = last
        if node != tree.root:
            tree.select_node(node)

    def action_toggle_detail(self) -> None:
        detail = self.query_one("#detail", Static)
        detail.display = not detail.display
        self._snap(f"detail panel {'shown' if detail.display else 'hidden'}")

    def action_yank_detail(self) -> None:
        self._snap("yank_detail")
        tree = self.query_one("#tree", Tree)
        node = tree.cursor_node
        data = self._get(node.data) if node else None
        if not data:
            return
        msgs = data.get("msgs") or []
        if not msgs:
            first_msg = data.get("first_msg")
            if first_msg:
                msgs = [first_msg]
        if not msgs:
            return
        result = _format_detail(msgs[0])
        for msg in msgs[1:]:
            result.append("\n\n")
            result.append_text(_format_detail(msg))
        self.copy_to_clipboard(result.plain)
        self.notify("Copied to clipboard")

    def action_focus_input(self) -> None:
        self.query_one("#chat-input", Input).focus()

    def action_edit_message(self) -> None:
        """Open $EDITOR to compose a message, then submit it."""
        import tempfile
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        chat_input = self.query_one("#chat-input", Input)
        # Pre-fill temp file with current input content
        with tempfile.NamedTemporaryFile(
            suffix=".md", mode="w", delete=False
        ) as f:
            f.write(chat_input.value)
            tmppath = f.name
        try:
            with self.app.suspend():
                subprocess.run([editor, tmppath])
            text = Path(tmppath).read_text().strip()
            if text:
                chat_input.value = text
                chat_input.focus()
        finally:
            Path(tmppath).unlink(missing_ok=True)

    def action_open_session(self) -> None:
        """Suspend the TUI and open the selected session in claude --resume."""
        tree = self.query_one("#tree", Tree)
        node = tree.cursor_node
        data = self._get(node.data) if node else None
        self._snap(f"open_session node={node and node.data} sids={data and data.get('session_ids', [])}")
        if not data:
            return
        sids = data.get("session_ids", [])
        if not sids:
            return
        session_id = sids[0]
        claude = shutil.which("claude")
        if not claude:
            self._update_status("Error: 'claude' not found in PATH")
            return

        with self.suspend():
            env = {k: v for k, v in os.environ.items()
                   if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
            subprocess.run(
                [claude, "--resume", session_id],
                env=env, cwd=self.cwd,
            )
        # Reload tree in case the session was modified
        self.load_tree(select_session=session_id)

    def action_reload(self) -> None:
        """Reload sessions from disk."""
        self._snap("reload")
        self._loading = True
        status = self.query_one("#status", Static)
        status.update(" Reloading sessions...")
        # Try to preserve position by re-selecting the current session
        tree = self.query_one("#tree", Tree)
        node = tree.cursor_node
        data = self._get(node.data) if node else None
        select = None
        if data and data.get("session_ids"):
            select = data["session_ids"][0]
        self.load_tree(select_session=select)

    # ------------------------------------------------------------------
    # Search (Helix-style: / to open, n/N to navigate, Esc to dismiss)
    # ------------------------------------------------------------------

    def action_search(self) -> None:
        self._snap("search opened")
        search = self.query_one("#search-input", Input)
        search.display = True
        search.value = ""
        search.focus()
        # Snapshot which nodes are currently expanded
        self._pre_search_expanded = self._snapshot_expanded()
        self._search_expanded: set = set()  # nodes we expanded for search

    def _snapshot_expanded(self) -> set:
        """Return set of node ids that are currently expanded."""
        tree = self.query_one("#tree", Tree)
        expanded = set()

        def _walk(node):
            if node.is_expanded and node != tree.root:
                expanded.add(id(node))
            for child in node.children:
                _walk(child)

        _walk(tree.root)
        return expanded

    def _restore_search_expanded(self) -> None:
        """Collapse nodes that were expanded only for search navigation."""
        for node in list(self._search_expanded):
            if node.is_expanded:
                node.collapse()
        self._search_expanded.clear()

    def _collect_nodes(self, expand_all: bool = False,
                       rg_sessions: set[str] | None = None):
        """Walk tree nodes in depth-first order.

        If *expand_all* is True, lazily populates collapsed nodes so that
        the entire tree is searchable.  Otherwise only visible (expanded)
        subtrees are walked.

        If *rg_sessions* is provided, subtrees whose sessions don't
        intersect are pruned during expansion (avoids populating nodes
        that can't match).
        """
        tree = self.query_one("#tree", Tree)
        nodes = []

        def _walk(node):
            if node != tree.root:
                nodes.append(node)
            if expand_all:
                # Prune: skip expanding subtrees with no matching sessions
                if rg_sessions is not None and node != tree.root:
                    data = self._get(node.data) if node.data and node.data != -1 else None
                    if data:
                        node_sessions = set(data.get("session_ids", []))
                        if not node_sessions & rg_sessions:
                            return
                self._populate_placeholder(node)
                for child in node.children:
                    _walk(child)
            elif node.is_expanded:
                for child in node.children:
                    _walk(child)

        _walk(tree.root)
        return nodes

    def _rg_matching_sessions(self, pattern: str) -> set[str] | None:
        """Use rg to find session files containing pattern.  Returns session IDs or None on error."""
        self._snap(f"_rg_matching_sessions pattern={pattern!r}")
        rg = shutil.which("rg") or shutil.which("grep")
        if not rg:
            return None
        is_grep = rg.endswith("grep")
        base = _sessions_dir()
        encoded = _encode_cwd(self.cwd)
        dirs = [d for d in base.iterdir() if d.is_dir() and d.name.startswith(encoded)]
        if not dirs:
            return set()
        try:
            if is_grep:
                cmd = [rg, "--files-with-matches", "--ignore-case",
                       "--recursive", "--include=*.jsonl", "-E", "--", pattern] + [str(d) for d in dirs]
            else:
                cmd = [rg, "--files-with-matches", "--ignore-case",
                       "--", pattern] + [str(d) for d in dirs]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode not in (0, 1):  # 1 = no matches
            return None
        sessions = set()
        for line in result.stdout.splitlines():
            p = Path(line)
            if p.suffix == ".jsonl":
                sessions.add(p.stem)
        return sessions

    def _rg_matching_hashes(self, pattern: str) -> tuple[set[str], set[str]] | None:
        """Use rg --line-number to find matches, then extract content hashes.

        Returns (session_ids, content_hashes) or None on error.
        Parses only the matching lines instead of entire files.
        """
        self._snap(f"_rg_matching_hashes pattern={pattern!r}")
        rg = shutil.which("rg")
        if not rg:
            return None
        base = _sessions_dir()
        encoded = _encode_cwd(self.cwd)
        dirs = [d for d in base.iterdir() if d.is_dir() and d.name.startswith(encoded)]
        if not dirs:
            return set(), set()
        try:
            cmd = [rg, "--line-number", "--ignore-case",
                   "--", pattern] + [str(d) for d in dirs]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode not in (0, 1):
            return None

        sessions: set[str] = set()
        hashes: set[str] = set()
        for line in result.stdout.splitlines():
            # Format: /path/to/session.jsonl:lineno:json_content
            colon1 = line.find(":")
            if colon1 < 0:
                continue
            fpath = line[:colon1]
            p = Path(fpath)
            if p.suffix == ".jsonl":
                sessions.add(p.stem)
            rest = line[colon1 + 1:]
            colon2 = rest.find(":")
            if colon2 < 0:
                continue
            json_str = rest[colon2 + 1:]
            try:
                rec = json.loads(json_str)
            except (json.JSONDecodeError, ValueError):
                continue
            # Compute content_hash the same way _parse_session_file does
            msg = rec.get("message", {})
            role = msg.get("role", "")
            content = msg.get("content", "")
            raw = json.dumps(content, sort_keys=True)
            h = hashlib.sha256(f"{role}:{raw}".encode()).hexdigest()[:16]
            hashes.add(h)
        return sessions, hashes

    def _node_matches(self, node, regex: re.Pattern,
                      rg_sessions: set[str] | None = None,
                      rg_hashes: set[str] | None = None) -> bool:
        """Check if a tree node matches the compiled regex."""
        if regex.search(node.label.plain):
            return True
        data = self._get(node.data) if node.data and node.data != -1 else None
        if not data:
            return False
        # Fast path: if rg found no matching sessions for this node, skip
        if rg_sessions is not None:
            node_sessions = set(data.get("session_ids", []))
            if not node_sessions & rg_sessions:
                return False
        # Fast path: check content_hash against rg results (avoids re-parsing).
        # Hashes may not match for coalesced messages, so fall through to regex.
        if rg_hashes is not None:
            for msg in data.get("msgs") or []:
                if msg.get("content_hash") in rg_hashes:
                    return True
            if not data.get("msgs"):
                first = data.get("first_msg")
                if first and first.get("content_hash") in rg_hashes:
                    return True
        # Regex match on content
        for msg in data.get("msgs") or []:
            text = _extract_text_content(msg.get("message", {}))
            if regex.search(text):
                return True
        if not data.get("msgs"):
            first = data.get("first_msg")
            if first:
                text = _extract_text_content(first.get("message", {}))
                if regex.search(text):
                    return True
        return False

    def _compile_pattern(self, pattern: str) -> re.Pattern | None:
        """Compile a search pattern as regex (case-insensitive), return None on bad regex."""
        try:
            return re.compile(pattern, re.IGNORECASE)
        except re.error:
            return None

    def _run_search(self, pattern: str, forward: bool = True) -> None:
        """Find all nodes matching pattern and jump to the first one."""
        self._snap(f"_run_search pattern={pattern!r}")
        if not pattern:
            self._search_matches = []
            self._search_index = -1
            self._search_pattern = ""
            return

        self._search_pattern = pattern
        regex = self._compile_pattern(pattern)
        if not regex:
            self._search_matches = []
            self._search_index = -1
            self._update_search_status()
            return
        nodes = self._collect_nodes(expand_all=False)

        # Incremental: match labels only (fast)
        self._search_matches = [
            n for n in nodes if regex.search(n.label.plain)
        ]

        if self._search_matches:
            # Start from nearest match after current cursor
            tree = self.query_one("#tree", Tree)
            cur = tree.cursor_node
            self._search_index = 0
            if cur:
                for i, m in enumerate(self._search_matches):
                    if m.id == cur.id:
                        self._search_index = i
                        break
                    # Pick first match after cursor position
                    if m.line > (cur.line if hasattr(cur, 'line') else 0):
                        self._search_index = i
                        break
            self._jump_to_match()
        else:
            self._search_index = -1

        self._update_search_status()

    def _jump_to_match(self) -> None:
        if not self._search_matches or self._search_index < 0:
            return
        # Collapse nodes we expanded for the previous match
        self._restore_search_expanded()

        tree = self.query_one("#tree", Tree)
        node = self._search_matches[self._search_index]
        pre = getattr(self, "_pre_search_expanded", set())
        # Expand ancestors so the node is visible, tracking what we expand
        ancestors = []
        p = node.parent
        while p is not None:
            ancestors.append(p)
            p = p.parent
        for a in reversed(ancestors):
            if not a.is_expanded:
                self._expand_node_now(a)
                if id(a) not in pre:
                    self._search_expanded.add(a)
        # Defer select+scroll so the tree layout reflects newly expanded nodes
        def _do_select():
            tree.select_node(node)
            tree.scroll_to_node(node)
        self.call_after_refresh(_do_select)

    def _update_search_status(self) -> None:
        if self._search_matches:
            self._update_status(
                f"/{self._search_pattern}  [{self._search_index + 1}/{len(self._search_matches)}]"
            )
        elif self._search_pattern:
            self._update_status(f"/{self._search_pattern}  [no matches]")

    def action_search_next(self) -> None:
        if not self._search_pattern:
            return
        self._refresh_matches(expand_all=True)
        if not self._search_matches:
            return
        self._search_index = (self._search_index + 1) % len(self._search_matches)
        self._jump_to_match()
        self._update_search_status()

    def action_search_prev(self) -> None:
        if not self._search_pattern:
            return
        self._refresh_matches(expand_all=True)
        if not self._search_matches:
            return
        self._search_index = (self._search_index - 1) % len(self._search_matches)
        self._jump_to_match()
        self._update_search_status()

    # --- Recent tips navigation (f / F) ---

    def _build_recent_tips(self) -> None:
        """Build list of session IDs sorted by most-recent tip first.

        Walks the trie (not the tree widget) to avoid populating nodes.
        """
        # Sort sessions by tip timestamp, most recent first
        sorted_sids = sorted(
            self.session_tips.keys(),
            key=lambda sid: self.session_tips[sid],
            reverse=True,
        )
        self._recent_tips = sorted_sids
        self._recent_index = -1

    def _jump_to_recent(self) -> None:
        if not self._recent_tips or self._recent_index < 0:
            return
        sid = self._recent_tips[self._recent_index]
        self._select_session(sid)

    def _update_recent_status(self) -> None:
        if self._recent_tips and self._recent_index >= 0:
            sid = self._recent_tips[self._recent_index]
            ts = self.session_tips.get(sid, "")
            age = _age_text(ts) if ts else "?"
            self._update_status(
                f"recent [{self._recent_index + 1}/{len(self._recent_tips)}]  ({age})  {sid[:8]}"
            )

    def action_recent_next(self) -> None:
        if not self._recent_tips:
            self._build_recent_tips()
        if not self._recent_tips:
            self._update_status("No session tips found")
            return
        self._recent_index = (self._recent_index + 1) % len(self._recent_tips)
        self._jump_to_recent()
        self._update_recent_status()

    def action_recent_prev(self) -> None:
        if not self._recent_tips:
            self._build_recent_tips()
        if not self._recent_tips:
            self._update_status("No session tips found")
            return
        self._recent_index = (self._recent_index - 1) % len(self._recent_tips)
        self._jump_to_recent()
        self._update_recent_status()

    def _resolve_to_child(self, node, regex: re.Pattern):
        """If node is a chain, populate children and return the child that matches.

        Only populates — does NOT expand or track.  _jump_to_match handles
        ancestor expansion so there's no collapse/re-expand race.
        """
        data = self._get(node.data) if node.data and node.data != -1 else None
        if not data or (data.get("msg_count", 1) <= 1):
            return node
        # Populate children without expanding (so _jump_to_match can manage it)
        self._populate_placeholder(node)
        # Find the child whose single message matches
        for child in node.children:
            if self._node_matches(child, regex):
                return child
        # Fallback: return the chain node itself
        return node

    def _refresh_matches(self, expand_all: bool = False) -> None:
        """Re-collect matches, preserving current position."""
        self._snap(f"_refresh_matches pattern={self._search_pattern!r} expand_all={expand_all}")
        regex = self._compile_pattern(self._search_pattern)
        if not regex:
            self._search_matches = []
            self._search_index = -1
            return
        rg_result = self._rg_matching_hashes(self._search_pattern)
        if rg_result is not None:
            rg_sessions, rg_hashes = rg_result
            self._snap(f"  rg: {len(rg_sessions)} sessions, {len(rg_hashes)} hashes")
        else:
            rg_sessions, rg_hashes = None, None
            self._snap("  rg: unavailable")
        nodes = self._collect_nodes(expand_all=expand_all, rg_sessions=rg_sessions)
        self._snap(f"  collected {len(nodes)} nodes")
        old_node = (self._search_matches[self._search_index]
                    if self._search_matches and 0 <= self._search_index < len(self._search_matches)
                    else None)
        raw = [n for n in nodes if self._node_matches(n, regex, rg_sessions, rg_hashes)]
        # Drill into chain nodes to find the specific child
        self._search_matches = [self._resolve_to_child(n, regex) for n in raw]
        # Deduplicate (a child may appear if both parent chain and child matched)
        seen = set()
        deduped = []
        for n in self._search_matches:
            if id(n) not in seen:
                seen.add(id(n))
                deduped.append(n)
        self._search_matches = deduped
        # Restore index to the previously selected node
        self._search_index = 0
        if old_node:
            for i, m in enumerate(self._search_matches):
                if m is old_node:
                    self._search_index = i
                    break

    def on_input_changed(self, event: Input.Changed) -> None:
        """Incremental search as user types."""
        if event.input.id == "search-input":
            self._run_search(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-input":
            # Confirm search — do full content search, keep expansion, dismiss
            search = self.query_one("#search-input", Input)
            search.display = False
            if self._search_pattern:
                self._refresh_matches(expand_all=True)
                if self._search_matches:
                    self._jump_to_match()
                self._update_search_status()
            self._search_expanded.clear()
            self.query_one("#tree", Tree).focus()
            return

        prompt = event.value.strip()
        if not prompt or self._streaming:
            return
        event.input.value = ""
        self._snap(f"chat submit: {prompt[:60]!r}")

        # Get session_id and content_hash from selected tree node (if any)
        tree = self.query_one("#tree", Tree)
        node = tree.cursor_node
        data = self._get(node.data) if node else None
        session_id = data["session_ids"][0] if data and data.get("session_ids") else None
        # Use last_msg hash (end of chain) for rewind target
        content_hash = None
        if data:
            msg = data.get("last_msg") or data.get("first_msg")
            if msg:
                content_hash = msg.get("content_hash")

        # If the selected node is a session tip (leaf with no children),
        # we can just resume without forking — no existing continuation to preserve.
        is_tip = node and not node.allow_expand and not data.get("_trie_node")
        needs_fork = not is_tip

        # Show the user's message in the tree immediately (before worker starts)
        self._add_pending_node(prompt)

        self._stream_chat(prompt, session_id, content_hash, needs_fork)

    def on_key(self, event) -> None:
        """Handle special keys from input widgets."""
        if event.key == "ctrl+e" and self.focused is self.query_one("#chat-input", Input):
            event.prevent_default()
            self.action_edit_message()
            return
        if event.key == "escape":
            search = self.query_one("#search-input", Input)
            if self.focused is search:
                search.display = False
                self._restore_search_expanded()
                self._search_matches = []
                self._search_index = -1
                self._search_pattern = ""
                self.query_one("#tree", Tree).focus()
                return
            if self.focused is self.query_one("#chat-input", Input):
                self.query_one("#tree", Tree).focus()


    @work(thread=True, group="stream_chat")
    def _stream_chat(self, prompt: str, session_id: str | None,
                     content_hash: str | None = None,
                     needs_fork: bool = True) -> None:
        claude = shutil.which("claude")
        if not claude:
            self.call_from_thread(self._update_status, "Error: 'claude' not found in PATH")
            return

        _log.info(f"_stream_chat start sid={session_id} hash={content_hash} fork={needs_fork}")
        self._streaming = True
        self.call_from_thread(self._update_status, "Streaming...")
        chat_input = self.query_one("#chat-input", Input)
        self.call_from_thread(setattr, chat_input, "disabled", True)

        # Fork-rewind-resume: fork the session, rewind the fork to the
        # selected message, then resume from there.
        # Skip fork when replying to a session tip (no existing continuation).
        resume_id = None
        if session_id and needs_fork:
            self.call_from_thread(self._update_status, "Forking session...")
            _log.info(f"forking {session_id}")
            fork_id = self._fork_session(claude, session_id)
            _log.info(f"fork done -> {fork_id}")
            if fork_id and content_hash:
                # Rewind the forked copy to the target message
                fork_path = _find_session_file(fork_id, self.cwd)
                _log.info(f"rewinding {fork_id} to {content_hash}")
                if fork_path and _rewind_session_file(fork_path, content_hash):
                    resume_id = fork_id
                    _log.info(f"rewind ok")
                else:
                    resume_id = fork_id  # rewind failed, resume from tip
                    _log.info(f"rewind failed, using tip")
            elif fork_id:
                resume_id = fork_id
            else:
                resume_id = session_id  # fork failed, resume original
                _log.info(f"fork failed, using original")
        elif session_id:
            # Replying to session tip — just resume directly
            resume_id = session_id
            _log.info(f"resuming tip session {session_id}")

        cmd = [
            claude, "--print", "--verbose", "--output-format", "stream-json",
            "--dangerously-skip-permissions",
        ]
        if resume_id:
            cmd.extend(["--resume", resume_id])
        cmd.append(prompt)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}

        try:
            _log.info(f"launching claude CLI")
            process = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, cwd=self.cwd,
            )

            text_so_far = ""
            for raw_line in process.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")
                if etype == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            text_so_far += block.get("text", "")
                            self.call_from_thread(self._update_response, text_so_far)
                        elif block.get("type") == "tool_use":
                            _log.info(f"tool_use: {block.get('name', '')}")
                            self.call_from_thread(
                                self._update_status,
                                f"Using tool: {block.get('name', '')}",
                            )
                elif etype == "result":
                    sid = event.get("session_id")
                    cost = event.get("total_cost_usd")
                    _log.info(f"result sid={sid and sid[:8]} cost={cost}")
                    self.call_from_thread(self._finish_response, sid, cost)

            process.wait()
            if process.returncode != 0:
                stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
                if stderr:
                    self.call_from_thread(self._update_status, f"Error: {stderr[:200]}")
        except Exception as exc:
            self.call_from_thread(self._update_status, f"Error: {exc}")
        finally:
            self._streaming = False
            self.call_from_thread(setattr, chat_input, "disabled", False)

    def _add_pending_node(self, prompt: str) -> None:
        """Add a temporary fork at the selected node showing the user's message.

        Splits the chain: selected node becomes a branch, existing
        continuation and the new message appear as children (visualizing
        the fork before the CLI is even invoked).
        """
        tree = self.query_one("#tree", Tree)
        node = tree.cursor_node
        if not node or node is tree.root:
            self._snap("_add_pending_node: no cursor node")
            return

        self._snap(f"_add_pending_node: prompt={prompt[:60]!r} node={node.data} "
                   f"allow_expand={node.allow_expand} label={node.label.plain[:40]}")
        self._populate_placeholder(node)

        # Save scroll position — nothing above the fork point changes
        scroll_y = tree.scroll_offset.y

        with self.batch_update():
            if not node.allow_expand:
                # Node is a leaf in a chain — check if it's a tip or mid-chain.
                parent = node.parent
                if not parent:
                    return

                siblings = list(parent.children)
                idx = next((i for i, s in enumerate(siblings) if s is node), None)
                if idx is None:
                    return
                after = siblings[idx + 1:]
                n_after = len(after)

                if n_after == 0:
                    # Tip node — no continuation to preserve, append as sibling.
                    fork_node = parent
                    _log.info(f"  tip node, appending as sibling")
                else:
                    # Mid-chain — split into fork with continuation + pending.
                    _log.info(f"  splitting chain at idx={idx}, {n_after} siblings after")

                    first_after_label = after[0].label
                    first_after_data = after[0].data

                    for s in siblings[idx:]:
                        s.remove()

                    fork_node = parent.add(node.label, data=node.data)

                    cont_label = Text()
                    cont_label.append(f"[{n_after} msgs] ", style="bold yellow")
                    cont_label.append_text(first_after_label)
                    cont = fork_node.add(cont_label, data=first_after_data)
                    cont.add_leaf(Text("...", style="dim"), data=-1)

                    _log.info(f"  fork_node created, 1 continuation + 1 pending")
            else:
                fork_node = node

            # Add the pending user message as a new fork branch
            label = Text()
            label.append("⏳ ", style="dim italic")
            label.append("👤: ", style="bold cyan")
            label.append(prompt)
            pending = fork_node.add_leaf(label, data=-2)  # -2 = pending sentinel

        def _expand():
            fork_node.expand()
            tree.scroll_to(0, scroll_y, animate=False)
            def _select():
                tree.select_node(pending)
                tree.scroll_to_node(pending)
                self._snap(f"  fork done, pending node selected")
            self.call_after_refresh(_select)
        self.call_after_refresh(_expand)

    def _reload_tree_inline(self, select_session: str | None = None) -> None:
        """Rebuild trie in current thread and render on main thread.

        Unlike load_tree (which uses @work), this avoids worker group
        cancellation issues when called during streaming.
        """
        self.trie_root, self.session_count, self.session_tips = _build_trie(self.cwd)
        self.call_from_thread(self._render_tree, select_session)

    def _fork_session(self, claude: str, session_id: str) -> str | None:
        """Fork a session via the CLI, returning the new session ID."""
        _log.info(f"_fork_session {session_id[:8]}")
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
        cmd = [
            claude, "--print", "--resume", session_id, "--fork-session",
            "--output-format", "json", "--dangerously-skip-permissions",
            ".",
        ]
        try:
            result = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, cwd=self.cwd,
                timeout=30,
            )
            if result.returncode != 0:
                return None
            stdout = result.stdout.decode("utf-8", errors="replace").strip()
            # Parse JSON output for session_id
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if "session_id" in data:
                        return data["session_id"]
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass
        return None

    def _update_response(self, text: str) -> None:
        detail = self.query_one("#detail", Static)
        detail.update(Text(text))

    def _update_status(self, text: str) -> None:
        status = self.query_one("#status", Static)
        status.update(f" {text}")

    def _finish_response(self, session_id: str | None, cost: float | None) -> None:
        self._snap(f"_finish_response sid={session_id and session_id[:8]} cost={cost}")
        parts = []
        if session_id:
            parts.append(f"session={session_id[:8]}")
        if cost is not None:
            parts.append(f"cost=${cost:.4f}")
        self._update_status(f"Done. {' '.join(parts)}")
        self.load_tree(select_session=session_id)

    def action_expand_all(self) -> None:
        self._snap("expand_all")
        tree = self.query_one("#tree", Tree)
        tree.root.expand_all()

    def action_collapse_all(self) -> None:
        self._snap("collapse_all")
        tree = self.query_one("#tree", Tree)
        tree.root.collapse_all()


VERSION = "0.1.0"


def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog="cctree",
        description="Interactive TUI for browsing Claude Code session trees.",
    )
    parser.add_argument("directory", nargs="?", default=str(Path.cwd()),
                        help="project directory to scan (default: cwd)")
    parser.add_argument("--log", action="store_true",
                        help="enable logging and screenshots to a temp directory")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args()

    log_dir = None
    if args.log:
        log_dir = _enable_logging()

    try:
        app = SessionTreeApp(args.directory)
        app.run()
    finally:
        if log_dir:
            print(f"Log directory: {log_dir}")


if __name__ == "__main__":
    main()
